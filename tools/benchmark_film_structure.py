#!/usr/bin/env python3
"""Run the frozen film-structure teaching benchmarks.

All synthetic material records are constructed in memory. Search requests use
the byte-preserved protocol in ``tests/fixtures/
film_structure_benchmark_protocol_20261011.json``; post-search training/holdout scoring is reported
separately from the optimizer's fixed budget.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import asdict, is_dataclass, replace
import hashlib
import json
import math
import platform
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping


WORKSPACE = Path(__file__).resolve().parents[1]
ARTIFACTS = WORKSPACE / "artifacts" / "film-optimization" / "needle-structure-20261011"
PROTOCOL_RELATIVE_PATH = "tests/fixtures/film_structure_benchmark_protocol_20261011.json"
PROTOCOL_PATH = WORKSPACE / PROTOCOL_RELATIVE_PATH
RESULTS_PATH = ARTIFACTS / "benchmark_results.json"
REPORT_PATH = ARTIFACTS / "benchmark_report.md"
INITIAL_BASELINE_PATH = ARTIFACTS / "benchmark_initial_baseline.json"
INITIAL_BASELINE_REPORT_PATH = ARTIFACTS / "benchmark_initial_baseline.md"
INVALIDATED_RESULTS_PATH = ARTIFACTS / "benchmark_invalidated_source_change.json"
COUNTEREXAMPLES_PATH = ARTIFACTS / "benchmark_counterexamples.json"
COUNTEREXAMPLES_REPORT_PATH = ARTIFACTS / "benchmark_counterexamples.md"
SOURCE_FINGERPRINT_FILES = (
    "AGENTS.md",
    "tools/benchmark_film_structure.py",
    "tests/test_film_structure_benchmarks.py",
    PROTOCOL_RELATIVE_PATH,
    "coat_sim/core/film_design.py",
    "coat_sim/core/film_design_service.py",
    "coat_sim/core/film_optimization.py",
    "coat_sim/core/film_optimization_derivatives.py",
    "coat_sim/core/film_optimization_needle.py",
    "coat_sim/core/film_optimization_structure.py",
    "coat_sim/core/film_optimization_scoring.py",
    "coat_sim/core/optical_constants.py",
)


def _runtime_import_paths() -> None:
    """Expose the project and use the CPython 3.14 wheels only as a fallback."""
    root = str(WORKSPACE)
    if root not in sys.path:
        sys.path.insert(0, root)

    # This bundle contains CPython 3.14 extensions.  In particular, do not put
    # it ahead of an environment's own NumPy when the benchmark runs on 3.12.
    if sys.version_info[:2] == (3, 14):
        try:
            import numpy  # noqa: F401
        except ImportError:
            bundled = WORKSPACE / "build" / "film_solver_deps_20261011"
            if bundled.is_dir() and str(bundled) not in sys.path:
                sys.path.insert(0, str(bundled))


def load_protocol(path: Path | None = None) -> dict[str, Any]:
    path = PROTOCOL_PATH if path is None else path
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if protocol.get("status") != "frozen_before_benchmark_runs":
        raise ValueError("benchmark protocol is not in its frozen pre-run state")
    if protocol.get("protocol_id") != "needle-structure-teaching-benchmarks-20261011-v1":
        raise ValueError("unexpected benchmark protocol id")
    if len(protocol.get("cases", ())) != 3:
        raise ValueError("the frozen protocol must contain AR, HR, and narrowband cases")
    return protocol


def source_fingerprint() -> dict[str, Any]:
    rows = {}
    for relative in SOURCE_FINGERPRINT_FILES:
        path = WORKSPACE / relative
        contents = path.read_bytes()
        rows[relative] = {
            "sha256": hashlib.sha256(contents).hexdigest(),
            "size_bytes": len(contents),
        }
    return rows


def _material_records(protocol: Mapping[str, Any]) -> list[dict[str, Any]]:
    _runtime_import_paths()
    from coat_sim.core.materials import default_material_profile

    optical_range = protocol["materials"]["constant_table_wavelength_range_nm"]
    materials = protocol["materials"]
    rows = []
    for role, material_id, set_id in (
        ("substrate", "structure_bench_substrate", "substrate_const"),
        ("L", "structure_bench_L", "L_const"),
        ("H", "structure_bench_H", "H_const"),
    ):
        optical = materials[role]
        record = default_material_profile(f"Frozen synthetic {role}", id=material_id)
        record.update({
            # Mirror the fields exposed by FilmMaterialLibrary._records while
            # keeping synthetic n/k data and all records in memory.
            "roles": ["substrate"] if role == "substrate" else ["film"],
            "design_ready": True,
            "notes": "Synthetic constant-index benchmark fixture; not production material data.",
            "optical_sets": [{
                "id": set_id,
                "name": f"constant n={optical['n']}",
                "kind": "tabulated",
                "notes": "Lossless constant-index fixture fixed by the benchmark protocol.",
                "source": "Frozen synthetic benchmark definition",
                "wavelength_unit": "nm",
                "wavelength_min": float(optical_range[0]),
                "wavelength_max": float(optical_range[1]),
                "wavelengths": [float(optical_range[0]), float(optical_range[1])],
                "n": [float(optical["n"]), float(optical["n"])],
                "k": [float(optical["k"]), float(optical["k"])],
            }],
        })
        rows.append(record)
    return rows


class _MemoryMaterialCatalog:
    """Tiny read-only material source. It deliberately has no filesystem path."""

    def __init__(self, records: Iterable[Mapping[str, Any]]):
        self._by_id = {row["id"]: copy.deepcopy(dict(row)) for row in records}

    def _records(self, material_ids: set[str] | None = None) -> list[dict[str, Any]]:
        if material_ids is None:
            selected = self._by_id.values()
        else:
            selected = (self._by_id[key] for key in sorted(material_ids) if key in self._by_id)
        return [copy.deepcopy(row) for row in selected]

    def get(self, material_id: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self._by_id[material_id])
        except KeyError as exc:
            raise ValueError(f"in-memory benchmark material is missing: {material_id}") from exc


def make_memory_service(protocol: Mapping[str, Any]):
    """Create a FilmDesignService-compatible optical service without persistence."""
    _runtime_import_paths()
    from coat_sim.core.film_design_service import FilmDesignService

    # Bypass __init__: it wires catalog paths. The methods used by optimization
    # and simulation below rely only on the in-memory material catalog.
    service = object.__new__(FilmDesignService)
    service.materials = _MemoryMaterialCatalog(_material_records(protocol))
    return service


def make_initial_design(service: Any, case: Mapping[str, Any]) -> dict[str, Any]:
    design = service.new_design()
    design["reference_wavelength_nm"] = float(
        case["method_controls"]["phase_reference_wavelength_nm"])
    design["substrate"].update(
        material_id="structure_bench_substrate",
        optical_set_id="substrate_const",
    )
    material_ids = {
        "L": ("structure_bench_L", "L_const"),
        "H": ("structure_bench_H", "H_const"),
    }
    design["front_layers"] = []
    for index, layer in enumerate(case["initial_front_stack"], start=1):
        material_id, optical_set_id = material_ids[layer["material"]]
        design["front_layers"].append({
            "id": f"{case['id']}-initial-{index}",
            "material_id": material_id,
            "optical_set_id": optical_set_id,
            "thickness": {"value": float(layer["thickness_nm"]), "unit": "nm"},
            "note": "Frozen benchmark initial layer.",
        })
    design["back_layers"] = []
    return service.validate(design)


def build_objectives(case: Mapping[str, Any]):
    _runtime_import_paths()
    from coat_sim.core.film_optimization import FilmOptimizationObjective

    training = case["training"]
    angles = tuple(float(value) for value in training["angles_deg"])
    polarizations = tuple(training["polarizations"])
    rows = []
    for band in training["bands"]:
        weight = float(band["total_band_weight"]) / (len(angles) * len(polarizations))
        for angle in angles:
            for polarization in polarizations:
                rows.append(FilmOptimizationObjective(
                    wavelength_min_nm=float(band["wavelength_nm"][0]),
                    wavelength_max_nm=float(band["wavelength_nm"][1]),
                    wavelength_samples=int(band["samples"]),
                    angles_deg=(angle,),
                    polarization=polarization,
                    metric=band["metric"],
                    mode="range",
                    lower_bound=float(band["lower_bound"]),
                    upper_bound=float(band["upper_bound"]),
                    weight=weight,
                    normalization_scale=float(band["normalization_scale"]),
                ))
    return tuple(rows)


def _response(service: Any, design: Mapping[str, Any], lo: float, hi: float,
              samples: int, angle: float, polarization: str) -> dict[str, Any]:
    settings = {
        "wavelength_min_nm": float(lo),
        "wavelength_max_nm": float(hi),
        "samples": int(samples),
        "angle_deg": float(angle),
        "polarization": polarization,
        "first_surface": "front",
    }
    return service.simulate(design, settings)


@contextmanager
def capture_jacobian_calls():
    """Count derivative-helper calls, separating value-only from Jacobian work."""
    _runtime_import_paths()
    from coat_sim.core import film_optimization_derivatives
    from coat_sim.core import film_optimization_structure

    original_derivative = film_optimization_derivatives.simulate_with_thickness_jacobian
    original_structure = film_optimization_structure.simulate_with_thickness_jacobian
    counter = {
        "calls": 0,
        "helper_calls": 0,
        "jacobian_evaluations": 0,
        "value_only_helper_calls": 0,
    }

    def counted(*args: Any, **kwargs: Any):
        counter["calls"] += 1
        counter["helper_calls"] += 1
        variables = kwargs.get("variables", args[2] if len(args) > 2 else ())
        if variables:
            counter["jacobian_evaluations"] += 1
        else:
            counter["value_only_helper_calls"] += 1
        return original_derivative(*args, **kwargs)

    film_optimization_derivatives.simulate_with_thickness_jacobian = counted
    film_optimization_structure.simulate_with_thickness_jacobian = counted
    try:
        yield counter
    finally:
        film_optimization_derivatives.simulate_with_thickness_jacobian = original_derivative
        film_optimization_structure.simulate_with_thickness_jacobian = original_structure


@contextmanager
def capture_legacy_probe_scores():
    """Count finite-insertion exact scores (excluding baseline and final audit)."""
    _runtime_import_paths()
    from coat_sim.core import film_optimization_needle

    original = film_optimization_needle._score_design
    counter = {"calls": 0}

    def counted(*args: Any, **kwargs: Any):
        counter["calls"] += 1
        return original(*args, **kwargs)

    film_optimization_needle._score_design = counted
    try:
        yield counter
    finally:
        film_optimization_needle._score_design = original


def score_grid(service: Any, design: Mapping[str, Any], case: Mapping[str, Any],
               *, holdout: bool) -> dict[str, Any]:
    """Score exact physical constraints, keeping search and holdout separate."""
    training = case["training"]
    if holdout:
        angle_groups = [
            ("target", tuple(case["holdout"]["target_angles_deg"])),
            ("stress_only", tuple(case["holdout"]["additional_stress_angles_deg"])),
        ]
        step = float(case["holdout"].get("bands", [{}])[0].get("step_nm", 0.25))
    else:
        angle_groups = [("target", tuple(training["angles_deg"]))]
        step = None

    rows = []
    all_hard_met = True
    max_normalized_violation = 0.0
    worst = None
    for band_index, band in enumerate(training["bands"]):
        if holdout:
            band_holdout = case["holdout"]["bands"][band_index]
            lo, hi = (float(value) for value in band_holdout["wavelength_nm"])
            step = float(band_holdout["step_nm"])
            samples = int(round((hi - lo) / step)) + 1
        else:
            lo, hi = (float(value) for value in band["wavelength_nm"])
            samples = int(band["samples"])
        for group_name, angles in angle_groups:
            for angle in angles:
                for polarization in training["polarizations"]:
                    response = _response(service, design, lo, hi, samples,
                                         float(angle), polarization)
                    wavelengths = [float(value) for value in response["wavelength_nm"]]
                    actual = [float(value) for value in response[band["metric"]]]
                    lower = float(band["lower_bound"])
                    upper = float(band["upper_bound"])
                    scale = float(band["normalization_scale"])
                    violations = [max(lower - value, value - upper, 0.0) for value in actual]
                    normalized = [value / scale for value in violations]
                    worst_index = max(range(len(actual)), key=lambda i: violations[i])
                    local_max = violations[worst_index]
                    local_normalized = normalized[worst_index]
                    hard_met = local_max <= 1e-14
                    if group_name == "target":
                        all_hard_met = all_hard_met and hard_met
                        if local_normalized > max_normalized_violation:
                            max_normalized_violation = local_normalized
                            worst = {
                                "band_index": band_index,
                                "wavelength_nm": wavelengths[worst_index],
                                "angle_deg": float(angle),
                                "polarization": polarization,
                                "metric": band["metric"],
                                "actual": actual[worst_index],
                                "physical_violation": local_max,
                                "normalized_violation": local_normalized,
                            }
                    rows.append({
                        "band_index": band_index,
                        "angle_role": group_name,
                        "angle_deg": float(angle),
                        "polarization": polarization,
                        "metric": band["metric"],
                        "wavelength_nm": [lo, hi],
                        "sample_count": samples,
                        "min_value": min(actual),
                        "max_value": max(actual),
                        "hard_target_met": hard_met,
                        "max_physical_violation": local_max,
                        "max_normalized_violation": local_normalized,
                        "worst_wavelength_nm": wavelengths[worst_index],
                    })
    # The best candidate always has zero violation for an empty collection,
    # but every frozen case includes at least one target row.
    return {
        "hard_target_met": all_hard_met,
        "max_normalized_violation": max_normalized_violation,
        "worst": worst,
        "conditions": rows,
    }


def score_additional_holdout_sweep(service: Any, design: Mapping[str, Any],
                                   case: Mapping[str, Any]) -> dict[str, Any] | None:
    """Report the narrowband case's wider 0.25 nm holdout sweep, not a target."""
    holdout = case["holdout"]
    if "additional_full_sweep_nm" not in holdout:
        return None
    import numpy as np

    lo, hi = (float(value) for value in holdout["additional_full_sweep_nm"])
    step = float(holdout["additional_full_sweep_step_nm"])
    samples = int(round((hi - lo) / step)) + 1
    summaries = []
    for angle in [*holdout["target_angles_deg"], *holdout["additional_stress_angles_deg"]]:
        for polarization in case["training"]["polarizations"]:
            response = _response(service, design, lo, hi, samples,
                                 float(angle), polarization)
            rows = {metric: np.asarray(response[metric], dtype=float) for metric in ("R", "T")}
            for metric, values in rows.items():
                index_min = int(np.argmin(values))
                index_max = int(np.argmax(values))
                summaries.append({
                    "angle_deg": float(angle),
                    "polarization": polarization,
                    "metric": metric,
                    "min_value": float(values[index_min]),
                    "min_wavelength_nm": float(response["wavelength_nm"][index_min]),
                    "max_value": float(values[index_max]),
                    "max_wavelength_nm": float(response["wavelength_nm"][index_max]),
                })
    return {
        "role": "diagnostic_holdout_only_not_part_of_target_met",
        "wavelength_nm": [lo, hi],
        "step_nm": step,
        "sample_count": samples,
        "conditions": summaries,
    }


def independent_airy_spectrum(layers: Iterable[Mapping[str, Any]],
                              material_indices: Mapping[str, float],
                              wavelengths_nm: Iterable[float],
                              *, n_ambient: float = 1.0,
                              n_substrate: float = 1.52) -> dict[str, list[float]]:
    """Independent lossless normal-incidence characteristic-matrix audit.

    ``layers`` uses the film design's stored front-layer order (substrate to
    incident medium). The characteristic matrix product is evaluated in
    physical incident-to-substrate order, so materialize and reverse the input
    exactly once before evaluating wavelengths.
    """
    import numpy as np

    n0 = complex(float(n_ambient), 0.0)
    ns = complex(float(n_substrate), 0.0)
    ordered_layers = tuple(reversed(tuple(layers)))
    result_r: list[float] = []
    result_t: list[float] = []
    for wave_value in wavelengths_nm:
        wave = float(wave_value)
        matrix = np.eye(2, dtype=complex)
        for layer in ordered_layers:
            n = complex(float(material_indices[layer["material"]]), 0.0)
            d = float(layer["thickness_nm"])
            phase = 2.0 * math.pi * n * d / wave
            cosine = np.cos(phase)
            sine = 1j * np.sin(phase)
            matrix = matrix @ np.array([
                [cosine, sine / n],
                [sine * n, cosine],
            ], dtype=complex)
        a = matrix[0, 0] + matrix[0, 1] * ns
        b = matrix[1, 0] + matrix[1, 1] * ns
        denominator = n0 * a + b
        reflection = (n0 * a - b) / denominator
        transmission = 2.0 * n0 / denominator
        reflectance = float(abs(reflection) ** 2)
        transmittance = float((ns.real / n0.real) * abs(transmission) ** 2)
        result_r.append(reflectance)
        result_t.append(transmittance)
    return {"R": result_r, "T": result_t}


def layer_sequence(design: Mapping[str, Any], protocol: Mapping[str, Any]) -> list[dict[str, Any]]:
    from coat_sim.core.film_design import physical_thickness_nm

    n_by_material = {
        "structure_bench_L": float(protocol["materials"]["L"]["n"]),
        "structure_bench_H": float(protocol["materials"]["H"]["n"]),
    }
    output = []
    for side in ("front", "back"):
        for row in design.get(f"{side}_layers", ()):
            output.append({
                "side": side,
                "layer_id": row["id"],
                "material_id": row["material_id"],
                "material": "L" if row["material_id"] == "structure_bench_L" else "H",
                "optical_set_id": row["optical_set_id"],
                "thickness_nm": float(physical_thickness_nm(
                    row, float(design.get("reference_wavelength_nm", 550.0)),
                    n_by_material[row["material_id"]])),
            })
    return output


def audit_limits(design: Mapping[str, Any], protocol: Mapping[str, Any]) -> dict[str, Any]:
    sequence = layer_sequence(design, protocol)
    limits = protocol["manufacturing_limits"]
    thicknesses = [float(row["thickness_nm"]) for row in sequence]
    violations = []
    for row in sequence:
        d = row["thickness_nm"]
        if d < float(limits["per_film_layer_min_nm"]) - 1e-8:
            violations.append({"kind": "layer_below_min", "layer_id": row["layer_id"], "value_nm": d})
        if d > float(limits["per_film_layer_max_nm"]) + 1e-8:
            violations.append({"kind": "layer_above_max", "layer_id": row["layer_id"], "value_nm": d})
    total = sum(thicknesses)
    if total > float(limits["total_physical_thickness_max_nm"]) + 1e-8:
        violations.append({"kind": "total_physical_thickness_above_max", "value_nm": total})
    if len(sequence) > int(limits["total_film_layer_count_max"]):
        violations.append({"kind": "layer_count_above_max", "value": len(sequence)})
    return {
        "feasible": not violations,
        "layer_count": len(sequence),
        "physical_total_thickness_nm": total,
        "per_layer_min_nm": min(thicknesses) if thicknesses else 0.0,
        "per_layer_max_nm": max(thicknesses) if thicknesses else 0.0,
        "violations": violations,
    }


def compare_project_to_airy(service: Any, design: Mapping[str, Any],
                            case: Mapping[str, Any],
                            protocol: Mapping[str, Any]) -> dict[str, Any]:
    import numpy as np

    holdout = case["holdout"]
    if "additional_full_sweep_nm" in holdout:
        lo, hi = (float(value) for value in holdout["additional_full_sweep_nm"])
        step = float(holdout["additional_full_sweep_step_nm"])
    else:
        lo = min(float(row["wavelength_nm"][0]) for row in holdout["bands"])
        hi = max(float(row["wavelength_nm"][1]) for row in holdout["bands"])
        step = float(holdout["bands"][0]["step_nm"])
    samples = int(round((hi - lo) / step)) + 1
    waves = np.linspace(lo, hi, samples)
    project = _response(service, design, lo, hi, samples, 0.0, "s")
    indices = {
        "L": float(protocol["materials"]["L"]["n"]),
        "H": float(protocol["materials"]["H"]["n"]),
    }
    sequence = layer_sequence(design, protocol)
    airy = independent_airy_spectrum(sequence, indices, waves,
                                      n_substrate=float(protocol["materials"]["substrate"]["n"]))
    project_r = np.asarray(project["R"], dtype=float)
    project_t = np.asarray(project["T"], dtype=float)
    airy_r = np.asarray(airy["R"], dtype=float)
    airy_t = np.asarray(airy["T"], dtype=float)
    delta_r = np.abs(project_r - airy_r)
    delta_t = np.abs(project_t - airy_t)
    return {
        "wavelength_nm": [lo, hi],
        "step_nm": step,
        "sample_count": samples,
        "project_kernel": "FilmDesignService.simulate, normal incidence, s polarization",
        "independent_kernel": "benchmark_film_structure.independent_airy_spectrum",
        "max_abs_R_delta": float(np.max(delta_r)),
        "rms_abs_R_delta": float(np.sqrt(np.mean(delta_r ** 2))),
        "max_abs_T_delta": float(np.max(delta_t)),
        "max_R_plus_T_energy_error_airy": float(np.max(np.abs(airy_r + airy_t - 1.0))),
        "worst_R_delta_wavelength_nm": float(waves[int(np.argmax(delta_r))]),
    }


def _base_request(service: Any, design: Mapping[str, Any], case: Mapping[str, Any],
                  protocol: Mapping[str, Any]):
    _runtime_import_paths()
    from coat_sim.core.film_optimization import (
        FilmOptimizationRequest,
        FilmThicknessVariable,
        freeze_film_optimization_request,
    )

    limits = protocol["manufacturing_limits"]
    objectives = build_objectives(case)
    variables = tuple(
        FilmThicknessVariable("front", row["id"],
                              float(limits["per_film_layer_min_nm"]),
                              float(limits["per_film_layer_max_nm"]))
        for row in design["front_layers"]
    )
    request = FilmOptimizationRequest(
        design_snapshot=copy.deepcopy(dict(design)),
        design_revision=1,
        variables=variables,
        objectives=objectives,
        max_iterations=120,
        max_evaluations=int(protocol["solver_and_budget"]["budget_per_method_per_case"]["training_grid_objective_evaluations_max"]),
        acceptable_normalized_error=float(protocol["solver_and_budget"]["acceptable_normalized_error"]),
        frozen_material_snapshot=None,
        solver="trf",
        seed=int(protocol["reproducibility"]["seed"]),
        multistart_count=1,
        validation_samples_multiplier=1,
    )
    return freeze_film_optimization_request(service, request)


def _candidate_material_references():
    _runtime_import_paths()
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    return (
        FilmNeedleMaterialReference("structure_bench_L", "L_const"),
        FilmNeedleMaterialReference("structure_bench_H", "H_const"),
    )


def _run_method(service: Any, base: Any, case: Mapping[str, Any],
                protocol: Mapping[str, Any], method_id: str) -> tuple[Any, dict[str, Any]]:
    _runtime_import_paths()
    from coat_sim.core.film_optimization import optimize_film_design

    budget = protocol["solver_and_budget"]["budget_per_method_per_case"]
    limits = protocol["manufacturing_limits"]
    if method_id == "fixed_trf":
        return optimize_film_design(service, base), {"search_time_isolated": True}

    if method_id == "legacy_finite_needle":
        from coat_sim.core.film_optimization_needle import (
            FilmNeedleOptimizationRequest,
            freeze_film_optimization_needle_request,
            optimize_film_design_with_needle,
        )
        controls = case["method_controls"]
        request = FilmNeedleOptimizationRequest(
            base_request=base,
            candidate_materials=_candidate_material_references(),
            max_added_layers=int(controls["legacy_max_added_layers"]),
            max_total_layers=int(limits["total_film_layer_count_max"]),
            min_thickness_nm=float(limits["per_film_layer_min_nm"]),
            max_thickness_nm=float(limits["per_film_layer_max_nm"]),
            insertion_trials_per_interface=1,
            seed=int(protocol["reproducibility"]["seed"]),
        )
        frozen = freeze_film_optimization_needle_request(service, request)
        with capture_legacy_probe_scores() as score_counter:
            result = optimize_film_design_with_needle(service, frozen)
        return result, {
            "search_time_isolated": True,
            "continuous_internal_position_sensitivity": False,
            "total_physical_thickness_search_cap": False,
            "candidate_materials_in_order": ["L", "H"],
            "insertion_trials_per_interface": 1,
            "finite_trial_thickness_nm": float(limits["per_film_layer_min_nm"]),
            "legacy_score_calls_are_trial_probes_or_final_validation": True,
            "legacy_score_design_calls": int(score_counter["calls"]),
            "exact_candidate_proposals": max(0, int(score_counter["calls"]) - int(
                getattr(getattr(result, "base_result", result), "validation_evaluations", 0) or 0)),
            "candidate_position_coverage": "interfaces only",
        }

    from coat_sim.core.film_optimization_structure import (
        FilmStructureOptimizationRequest,
        freeze_film_structure_optimization_request,
        optimize_film_design_with_structure,
    )
    modes = {
        "classical_needle_only": "needle_only",
        "growth_only": "growth_only",
        "growth_then_needle": "combined",
    }
    mode = modes[method_id]
    controls = case["method_controls"]
    initial_layer_count = len(case["initial_front_stack"])
    planned_position_sites = initial_layer_count * int(
        controls["structure_position_samples_per_host_layer"]) + initial_layer_count + 1
    request = FilmStructureOptimizationRequest(
        base_request=replace(base, solver="baseline"),
        candidate_materials=_candidate_material_references(),
        mode=mode,
        max_total_thickness_nm=float(limits["total_physical_thickness_max_nm"]),
        min_thickness_nm=float(limits["per_film_layer_min_nm"]),
        max_thickness_nm=float(limits["per_film_layer_max_nm"]),
        max_total_layers=int(limits["total_film_layer_count_max"]),
        max_added_layers=int(controls["structure_max_added_layers"]),
        position_samples=int(controls["structure_position_samples_per_host_layer"]),
        candidate_budget=int(controls["candidate_budget"]),
        needle_thickness_samples=int(controls["needle_thickness_samples"]),
        growth_steps_nm=tuple(float(value) for value in controls["growth_steps_nm"]),
        growth_phase_fractions=tuple(float(value) for value in controls["growth_phase_fractions"]),
        growth_cycles=int(controls["growth_cycles"]),
        seed=int(protocol["reproducibility"]["seed"]),
    )
    frozen = freeze_film_structure_optimization_request(service, request)
    result = optimize_film_design_with_structure(service, frozen)
    return result, {
        "search_time_isolated": True,
        "structure_mode": mode,
        "candidate_budget": int(controls["candidate_budget"]),
        "position_samples_per_host_layer": int(controls["structure_position_samples_per_host_layer"]),
        "planned_position_sites_before_materials": planned_position_sites,
        "planned_position_material_sites": planned_position_sites * len(request.candidate_materials),
        "needle_thickness_samples": int(controls["needle_thickness_samples"]),
        "needle_thickness_grid_nm": [
            float(limits["per_film_layer_min_nm"])
            + i * (float(limits["per_film_layer_max_nm"]) - float(limits["per_film_layer_min_nm"]))
            / (int(controls["needle_thickness_samples"]) - 1)
            for i in range(int(controls["needle_thickness_samples"]))
        ],
        "growth_steps_nm": list(controls["growth_steps_nm"]),
        "growth_phase_fractions": list(controls["growth_phase_fractions"]),
        "growth_phase_reference_wavelength_nm": float(controls["phase_reference_wavelength_nm"]),
        "growth_cycles": int(controls["growth_cycles"]),
        "max_added_layers": int(controls["structure_max_added_layers"]),
    }


def _unwrap_base_result(result: Any) -> Any:
    return getattr(result, "base_result", result)


def _best_design(result: Any) -> Mapping[str, Any]:
    base = _unwrap_base_result(result)
    return base.best_design


def _run_stats(result: Any, method_id: str) -> dict[str, Any]:
    base = _unwrap_base_result(result)
    counters = getattr(result, "counters", None)
    def count(name: str, fallback: Any = None) -> int | None:
        value = getattr(counters, name, None) if counters is not None else None
        if value is None:
            value = fallback
        return _json_int(value)

    objective_evaluations = count("objective_evaluations", getattr(base, "evaluations", None))
    validation_evaluations = count("validation_evaluations", getattr(base, "validation_evaluations", None))
    forward_equivalent_evaluations = objective_evaluations
    if counters is None and objective_evaluations is not None and validation_evaluations is not None:
        forward_equivalent_evaluations += validation_evaluations
    return {
        "state": str(getattr(base, "state", getattr(result, "state", "unknown"))),
        "termination_reason": str(getattr(base, "termination_reason", "")),
        "algorithm_targets_met": bool(getattr(base, "targets_met", False)),
        "algorithm_max_normalized_error": _json_float(getattr(base, "max_normalized_error", None)),
        "algorithm_weighted_rms_error": _json_float(getattr(base, "weighted_rms_error", None)),
        "objective_evaluations": objective_evaluations,
        # Structure counters already include coarse validation passes in the
        # shared count. Fixed/legacy API evaluation counts exclude validation,
        # so include the separate validation count in this workload proxy.
        "forward_equivalent_evaluations": forward_equivalent_evaluations,
        "score_evaluations": count("score_evaluations", getattr(result, "score_evaluations", None)),
        "validation_evaluations": validation_evaluations,
        "jacobian_evaluations": count("jacobian_evaluations", getattr(result, "jacobian_evaluations", None)),
        "structural_candidate_count": count("candidate_count", getattr(result, "candidate_count", None)),
        "needle_probe_count": count("needle_probe_count"),
        "growth_proposal_count": count("growth_proposal_count"),
        "local_iterations": count("local_iterations", getattr(base, "iterations", None)),
        "failed_evaluations": count("failed_evaluations", getattr(base, "failed_evaluations", 0)),
        "candidate_budget_exhausted": bool(getattr(counters, "candidate_budget_exhausted", False)),
        "evaluation_budget_exhausted": bool(getattr(counters, "evaluation_budget_exhausted", False)),
        "accepted_insertions": _json_int(getattr(result, "accepted_insertions", None)),
        "rolled_back_insertions": _json_int(getattr(result, "rolled_back_insertions", None)),
        "added_layer_ids": list(getattr(result, "added_layer_ids", ())),
        "method_id": method_id,
    }


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return _json_float(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _structure_stage_evidence(trace: Any) -> dict[str, Any]:
    events = trace if isinstance(trace, list) else []
    growth = [row for row in events if "growth" in str(row.get("stage", "")).lower()
              or "growth" in str(row.get("candidate_kind", "")).lower()]
    needle = [row for row in events if "needle" in str(row.get("stage", "")).lower()
              or "needle" in str(row.get("candidate_kind", "")).lower()]
    accepted_growth = [row for row in growth if row.get("accepted")]
    accepted_needle = [row for row in needle if row.get("accepted")]
    return {
        "growth_event_count": len(growth),
        "needle_event_count": len(needle),
        "accepted_growth_count": len(accepted_growth),
        "accepted_needle_count": len(accepted_needle),
        "accepted_growth_sequence_numbers": [row.get("sequence") for row in accepted_growth],
        "accepted_needle_sequence_numbers": [row.get("sequence") for row in accepted_needle],
        "accepted_growth_precedes_accepted_needle": bool(
            accepted_growth and accepted_needle and
            max(int(row.get("sequence", 0)) for row in accepted_growth) <
            min(int(row.get("sequence", 0)) for row in accepted_needle)),
        "interpretation": "Trace order is necessary evidence for staged growth then needle, but by itself does not establish a cross-valley improvement; inspect the event scores and compare to the initial/best scores.",
    }


def _json_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _json_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _method_error(case_id: str, method_id: str, error: BaseException,
                  wall_seconds: float) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "method_id": method_id,
        "state": "blocked_or_failed",
        "termination_reason": type(error).__name__,
        "error": str(error),
        "wall_clock_seconds": wall_seconds,
        "hard_target_met_training": False,
        "hard_target_met_holdout": False,
        "objective_evaluations": None,
        "forward_equivalent_evaluations": None,
        "jacobian_evaluations": None,
        "structural_candidate_count": None,
    }


def run_benchmarks(protocol: Mapping[str, Any] | None = None) -> dict[str, Any]:
    _runtime_import_paths()
    import numpy as np
    import scipy

    protocol = load_protocol() if protocol is None else dict(protocol)
    fingerprint_before = source_fingerprint()
    service = make_memory_service(protocol)
    method_ids = [row["id"] for row in protocol["methods"]]
    run_rows = []
    for case in protocol["cases"]:
        design = make_initial_design(service, case)
        base = _base_request(service, design, case, protocol)
        for method_id in method_ids:
            started = time.perf_counter()
            try:
                with capture_jacobian_calls() as jacobian_counter:
                    result, support = _run_method(service, base, case, protocol, method_id)
                elapsed = time.perf_counter() - started
                support["derivative_helper_calls_instrumented"] = int(jacobian_counter["helper_calls"])
                support["jacobian_calls_instrumented"] = int(jacobian_counter["jacobian_evaluations"])
                support["value_only_helper_calls_instrumented"] = int(jacobian_counter["value_only_helper_calls"])
                best = _best_design(result)
                stats = _run_stats(result, method_id)
                reported_jacobians = stats["jacobian_evaluations"]
                if reported_jacobians is None:
                    reported_jacobians = int(jacobian_counter["jacobian_evaluations"])
                candidate_count = stats["structural_candidate_count"]
                if candidate_count is None and "exact_candidate_proposals" in support:
                    candidate_count = int(support["exact_candidate_proposals"])
                trace = _json_safe(getattr(
                    result, "trace", getattr(result, "events", getattr(result, "search_trace", None))))
                stage_evidence = _structure_stage_evidence(trace)
                topology_audit = _json_safe(getattr(result, "topology_audit", None))
                coverage = (topology_audit.get("needle_scan") if isinstance(topology_audit, Mapping)
                            else getattr(result, "position_coverage", None))
                # These post-run audits are deliberately outside the fixed
                # optimizer budget and are reported separately.
                training = score_grid(service, best, case, holdout=False)
                holdout = score_grid(service, best, case, holdout=True)
                airy = compare_project_to_airy(service, best, case, protocol)
                audit = audit_limits(best, protocol)
                base_result = _unwrap_base_result(result)
                run_rows.append({
                    "case_id": case["id"],
                    "method_id": method_id,
                    "state": stats["state"],
                    "termination_reason": stats["termination_reason"],
                    "hard_target_met_training": training["hard_target_met"],
                    "hard_target_met_holdout": holdout["hard_target_met"],
                    "training_max_normalized_violation": training["max_normalized_violation"],
                    "holdout_max_normalized_violation": holdout["max_normalized_violation"],
                    "training_worst": training["worst"],
                    "holdout_worst": holdout["worst"],
                    "algorithm_targets_met": stats["algorithm_targets_met"],
                    "algorithm_max_normalized_error": stats["algorithm_max_normalized_error"],
                    "algorithm_weighted_rms_error": stats["algorithm_weighted_rms_error"],
                    "initial_layer_sequence": layer_sequence(design, protocol),
                    "best_layer_sequence": layer_sequence(best, protocol),
                    "layer_limits_audit": audit,
                    "objective_evaluations": stats["objective_evaluations"],
                    "forward_equivalent_evaluations": stats["forward_equivalent_evaluations"],
                    "score_evaluations": stats["score_evaluations"],
                    "validation_evaluations_reported_by_optimizer": stats["validation_evaluations"],
                    "jacobian_evaluations": reported_jacobians,
                    "jacobian_evaluations_from_result": stats["jacobian_evaluations"],
                    "structural_candidate_count": candidate_count,
                    "position_coverage": copy.deepcopy(coverage),
                    "topology_audit": topology_audit,
                    "accepted_insertions": stats["accepted_insertions"],
                    "rolled_back_insertions": stats["rolled_back_insertions"],
                    "failed_evaluations": stats["failed_evaluations"],
                    "added_layer_ids": stats["added_layer_ids"],
                    "iteration_count": stats["local_iterations"],
                    "wall_clock_seconds": elapsed,
                    "budget": protocol["solver_and_budget"]["budget_per_method_per_case"],
                    "budget_observed_within_caps": (
                        stats["objective_evaluations"] is None or
                        stats["objective_evaluations"] <= int(protocol["solver_and_budget"]["budget_per_method_per_case"]["training_grid_objective_evaluations_max"])
                    ),
                    "support_notes": support,
                    "search_trace": trace,
                    "growth_then_needle_trace_evidence": stage_evidence,
                    "independent_airy_check": airy,
                    "training_audit_conditions": training["conditions"],
                    "holdout_audit_conditions": holdout["conditions"],
                    "additional_holdout_sweep": score_additional_holdout_sweep(
                        service, best, case),
                })
            except Exception as exc:
                elapsed = time.perf_counter() - started
                run_rows.append(_method_error(case["id"], method_id, exc, elapsed))
    fingerprint_after = source_fingerprint()
    return {
        "protocol_id": protocol["protocol_id"],
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "execution": protocol["reproducibility"]["execution"],
        "runtime": {
            "python": sys.version,
            "platform": platform.platform(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
        "method_count": len(method_ids),
        "case_count": len(protocol["cases"]),
        "source_fingerprint_before": fingerprint_before,
        "source_fingerprint_after": fingerprint_after,
        "source_fingerprint_stable_for_entire_run": fingerprint_before == fingerprint_after,
        "runs": run_rows,
    }


def run_initial_baseline_audits(protocol: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Score only each frozen initial stack; this never invokes an optimizer."""
    _runtime_import_paths()
    protocol = load_protocol() if protocol is None else dict(protocol)
    service = make_memory_service(protocol)
    rows = []
    for case in protocol["cases"]:
        design = make_initial_design(service, case)
        training = score_grid(service, design, case, holdout=False)
        holdout = score_grid(service, design, case, holdout=True)
        rows.append({
            "case_id": case["id"],
            "method_id": "initial_unoptimized_stack",
            "state": "scored_no_optimization",
            "initial_layer_sequence": layer_sequence(design, protocol),
            "layer_limits_audit": audit_limits(design, protocol),
            "hard_target_met_training": training["hard_target_met"],
            "hard_target_met_holdout": holdout["hard_target_met"],
            "training_max_normalized_violation": training["max_normalized_violation"],
            "holdout_max_normalized_violation": holdout["max_normalized_violation"],
            "training_worst": training["worst"],
            "holdout_worst": holdout["worst"],
            "training_audit_conditions": training["conditions"],
            "holdout_audit_conditions": holdout["conditions"],
            "independent_airy_check": compare_project_to_airy(service, design, case, protocol),
            "additional_holdout_sweep": score_additional_holdout_sweep(service, design, case),
            "optimization_invoked": False,
            "budget_consumed": 0,
        })
    return {
        "protocol_id": protocol["protocol_id"],
        "purpose": "Pre-optimizer baseline only; no method is optimized and no target/budget is selected from these results.",
        "runs": rows,
    }


def _evaluate_finite_width_diagnostic(evaluator: Any, frozen: Any,
                                     design: Mapping[str, Any]) -> Any:
    """Get a finite-candidate SSE with no variable-specific Jacobian work.

    The pure structure evaluator returns ``smooth_loss=None`` when called with
    ``with_jacobian=False``. Requesting a Jacobian with an empty variable list
    asks it to provide the scalar objective while evaluating no nonempty
    variable Jacobian.
    """
    return evaluator(frozen, design, with_jacobian=True, variable_ids=())


def run_counterexample_diagnostics(protocol: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run the separately budgeted, non-ranking sensitivity/finite-width checks."""
    _runtime_import_paths()
    from coat_sim.core.film_optimization_structure import (
        FilmStructureOptimizationRequest,
        _feasible_design,
        _insert_at_site,
        _site_records,
        _stable_identifier,
        evaluate_frozen_structure_candidate,
        freeze_film_structure_optimization_request,
    )

    protocol = load_protocol() if protocol is None else dict(protocol)
    diagnostic_controls = protocol["counterexample_diagnostics"]
    grid_budget = int(diagnostic_controls["budget_per_case"]["objective_grid_evaluations_max"])
    limits = protocol["manufacturing_limits"]
    service = make_memory_service(protocol)
    case_rows = []
    fingerprint_before = source_fingerprint()
    for case in protocol["cases"]:
        controls = case["method_controls"]
        initial = make_initial_design(service, case)
        base = _base_request(service, initial, case, protocol)
        references = _candidate_material_references()
        request = FilmStructureOptimizationRequest(
            base_request=replace(base, solver="baseline"),
            candidate_materials=references,
            mode="needle_only",
            max_total_thickness_nm=float(limits["total_physical_thickness_max_nm"]),
            min_thickness_nm=float(limits["per_film_layer_min_nm"]),
            max_thickness_nm=float(limits["per_film_layer_max_nm"]),
            max_total_layers=int(limits["total_film_layer_count_max"]),
            max_added_layers=1,
            position_samples=int(controls["structure_position_samples_per_host_layer"]),
            candidate_budget=grid_budget,
            needle_thickness_samples=int(controls["needle_thickness_samples"]),
            growth_steps_nm=tuple(float(v) for v in controls["growth_steps_nm"]),
            growth_phase_fractions=tuple(float(v) for v in controls["growth_phase_fractions"]),
            growth_cycles=1,
            seed=int(protocol["reproducibility"]["seed"]),
        )
        frozen = freeze_film_structure_optimization_request(service, request)
        design = copy.deepcopy(dict(frozen.base_request.design_snapshot))
        materials = frozen.base_request.frozen_material_snapshot
        sites = _site_records(design, materials, frozen.position_samples)
        planned_pairs = [(site, reference) for site in sites for reference in references]
        width_grid = [
            float(limits["per_film_layer_min_nm"])
            + index * (float(limits["per_film_layer_max_nm"]) -
                       float(limits["per_film_layer_min_nm"]))
            / (int(controls["needle_thickness_samples"]) - 1)
            for index in range(int(controls["needle_thickness_samples"]))
        ]
        output = {
            "case_id": case["id"],
            "state": "running_diagnostic",
            "search_method_ranked": False,
            "target_acceptance_changed": False,
            "planned_zero_thickness_position_material_probes": len(planned_pairs),
            "planned_finite_width_scores_max": 18,
            "evaluation_budget_max": grid_budget,
            "evaluation_count": 0,
            "failed_evaluations": 0,
            "zero_thickness_probes": [],
            "selected_interior_probe": None,
            "selected_interface_probe": None,
            "interior_more_downhill_than_interface": None,
            "finite_width_checks": [],
            "finite_width_derivative_reversal_found": False,
            "exact_score_rank_reversal_found": False,
            "derivative_preferred_class": None,
            "best_finite_exact_score_class": None,
            "fully_scanned": True,
            "unscanned_probe_count": 0,
        }
        probe_rows = []
        baseline_score = None
        for pair_index, (site, reference) in enumerate(planned_pairs):
            if output["evaluation_count"] >= grid_budget:
                output["fully_scanned"] = False
                output["unscanned_probe_count"] = len(planned_pairs) - pair_index
                break
            existing = {row["id"] for row in design["front_layers"]}
            identity = [case["id"], site["kind"], site["interface"],
                        site.get("host_layer_id", ""), site.get("fraction"),
                        reference.material_id, reference.optical_set_id]
            needle_id = _stable_identifier("benchmark-diagnostic-needle", identity, existing)
            split_id = _stable_identifier("benchmark-diagnostic-split", identity, existing | {needle_id})
            zero_design = _insert_at_site(
                design, site, reference, 0.0, needle_id, split_id, zero=True)
            output["evaluation_count"] += 1
            try:
                evaluated = evaluate_frozen_structure_candidate(
                    frozen, zero_design, variable_ids=(needle_id,), with_jacobian=True,
                    zero_thickness_layer_ids=(needle_id,),
                )
            except (ValueError, ArithmeticError, FloatingPointError, RuntimeError, KeyError) as exc:
                output["failed_evaluations"] += 1
                output["fully_scanned"] = False
                output["zero_thickness_probes"].append({
                    "site_kind": site["kind"], "interface": site["interface"],
                    "host_layer_id": site.get("host_layer_id", ""),
                    "position_nm": float(site["position_nm"]),
                    "material_id": reference.material_id,
                    "state": "failed", "error_type": type(exc).__name__,
                })
                continue
            if baseline_score is None:
                baseline_score = evaluated["score"]
            feasible_widths = []
            infeasible_widths = []
            for width in width_grid:
                finite_design = _insert_at_site(
                    design, site, reference, width, needle_id, split_id, zero=False)
                okay, why = _feasible_design(frozen, finite_design, materials)
                (feasible_widths if okay else infeasible_widths).append(
                    {"width_nm": width} if okay else {"width_nm": width, "reason": why})
            probe = {
                "site_kind": site["kind"],
                "interface": int(site["interface"]),
                "host_layer_id": str(site.get("host_layer_id", "")),
                "position_nm": float(site["position_nm"]),
                "position_fraction": (float(site["fraction"])
                                      if site.get("fraction") is not None else None),
                "material_id": reference.material_id,
                "needle_layer_id": needle_id,
                "gradient_smooth_loss_per_nm": float(evaluated["gradient_by_layer_id"][needle_id]),
                "zero_probe_smooth_loss": float(evaluated["smooth_loss"]),
                "score": _json_safe(evaluated["score"]),
                "feasible_widths_nm": [row["width_nm"] for row in feasible_widths],
                "infeasible_widths": infeasible_widths,
                "state": "scored_zero_thickness_only",
            }
            output["zero_thickness_probes"].append(probe)
            if feasible_widths:
                probe_rows.append(probe)

        for kind, key in (("interior", "selected_interior_probe"),
                          ("interface", "selected_interface_probe")):
            candidates = [row for row in probe_rows if row["site_kind"] == kind]
            if candidates:
                candidates.sort(key=lambda row: (
                    row["gradient_smooth_loss_per_nm"],
                    next(i for i, ref in enumerate(references) if ref.material_id == row["material_id"]),
                    row["position_nm"],
                ))
                output[key] = copy.deepcopy(candidates[0])
        interior = output["selected_interior_probe"]
        interface = output["selected_interface_probe"]
        if interior is not None and interface is not None:
            output["interior_more_downhill_than_interface"] = (
                float(interior["gradient_smooth_loss_per_nm"]) <
                float(interface["gradient_smooth_loss_per_nm"]))

        selected = [row for row in (interior, interface) if row is not None]
        for probe in selected:
            site = next(row for row in sites if
                        row["kind"] == probe["site_kind"] and
                        int(row["interface"]) == probe["interface"] and
                        str(row.get("host_layer_id", "")) == probe["host_layer_id"] and
                        row.get("fraction") == probe["position_fraction"] and
                        math.isclose(float(row["position_nm"]), float(probe["position_nm"]), abs_tol=1e-10))
            reference = next(ref for ref in references if ref.material_id == probe["material_id"])
            needle_id = str(probe["needle_layer_id"])
            split_id = _stable_identifier("benchmark-diagnostic-split", [
                case["id"], site["kind"], site["interface"], site.get("host_layer_id", ""),
                site.get("fraction"), reference.material_id, reference.optical_set_id],
                {row["id"] for row in design["front_layers"]} | {needle_id})
            checks = []
            for width in probe["feasible_widths_nm"]:
                if output["evaluation_count"] >= grid_budget:
                    output["fully_scanned"] = False
                    break
                finite_design = _insert_at_site(
                    design, site, reference, float(width), needle_id, split_id, zero=False)
                okay, reason = _feasible_design(frozen, finite_design, materials)
                if not okay:
                    # Recheck immediately before optical scoring; a width that
                    # is no longer physically feasible is recorded, never scored.
                    checks.append({"width_nm": float(width), "state": "infeasible", "reason": reason})
                    continue
                output["evaluation_count"] += 1
                try:
                    evaluated = _evaluate_finite_width_diagnostic(
                        evaluate_frozen_structure_candidate, frozen, finite_design)
                except (ValueError, ArithmeticError, FloatingPointError, RuntimeError, KeyError) as exc:
                    output["failed_evaluations"] += 1
                    checks.append({"width_nm": float(width), "state": "failed",
                                   "error_type": type(exc).__name__})
                    continue
                baseline_loss = float(probe["zero_probe_smooth_loss"])
                baseline_key = tuple(float(value) for value in probe["score"]["key"])
                candidate_key = tuple(float(value) for value in evaluated["score"]["key"])
                smooth_delta = float(evaluated["smooth_loss"]) - baseline_loss
                key_delta = [candidate_key[0] - baseline_key[0], candidate_key[1] - baseline_key[1]]
                row = {
                    "width_nm": float(width),
                    "state": "exact_scored_finite_width",
                    "physically_feasible": True,
                    "smooth_loss": float(evaluated["smooth_loss"]),
                    "smooth_loss_delta_from_zero_probe": smooth_delta,
                    "max_normalized_error": float(evaluated["score"]["max_error"]),
                    "weighted_rms_error": float(evaluated["score"]["weighted_rms_error"]),
                    "exact_score_key": list(candidate_key),
                    "exact_score_key_delta_from_baseline": key_delta,
                    "exact_score_improved": candidate_key < baseline_key,
                }
                if float(probe["gradient_smooth_loss_per_nm"]) < 0 and smooth_delta > 1e-14:
                    row["derivative_sign_reversal"] = True
                    output["finite_width_derivative_reversal_found"] = True
                else:
                    row["derivative_sign_reversal"] = False
                checks.append(row)
            output["finite_width_checks"].append({
                "site_kind": probe["site_kind"],
                "position_nm": probe["position_nm"],
                "material_id": probe["material_id"],
                "gradient_smooth_loss_per_nm": probe["gradient_smooth_loss_per_nm"],
                "width_results": checks,
            })
        if interior is not None and interface is not None:
            if output["interior_more_downhill_than_interface"] is True:
                output["derivative_preferred_class"] = "interior"
            elif output["interior_more_downhill_than_interface"] is False:
                output["derivative_preferred_class"] = "interface"
            best_key_by_class = {}
            for group in output["finite_width_checks"]:
                scored = [row for row in group["width_results"]
                          if row.get("state") == "exact_scored_finite_width"]
                if scored:
                    best_key_by_class[group["site_kind"]] = min(
                        tuple(row["exact_score_key"]) for row in scored)
            if best_key_by_class:
                output["best_finite_exact_score_class"] = min(
                    best_key_by_class, key=best_key_by_class.get)
            preferred = output["derivative_preferred_class"]
            actual = output["best_finite_exact_score_class"]
            output["exact_score_rank_reversal_found"] = bool(
                preferred is not None and actual is not None and preferred != actual)
        output["state"] = "completed" if output["fully_scanned"] else "partial_budget_or_failure"
        case_rows.append(output)
    fingerprint_after = source_fingerprint()
    return {
        "protocol_id": protocol["protocol_id"],
        "purpose": diagnostic_controls["purpose"],
        "source_fingerprint_before": fingerprint_before,
        "source_fingerprint_after": fingerprint_after,
        "source_fingerprint_stable_for_entire_run": fingerprint_before == fingerprint_after,
        "cases": case_rows,
    }


def write_counterexample_report(results: Mapping[str, Any],
                                path: Path = COUNTEREXAMPLES_REPORT_PATH) -> None:
    lines = [
        "# Non-ranking needle counterexample diagnostics",
        "",
        "These separate 128-evaluation diagnostics do not rank the five methods or change target decisions. Exact zero-thickness probes are mathematical sensitivities only; finite widths are scored only after physical-limit checks.",
        "",
        "| Case | Full zero-scan | Probes | Interior vs interface more downhill | Finite-width derivative reversal | Exact score rank reversal | Evaluations |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in results.get("cases", []):
        lines.append("| {case} | {full} | {probes}/{planned} | {interior} | {finite} | {exact} | {evals}/128 |".format(
            case=row.get("case_id", ""), full=row.get("fully_scanned"),
            probes=len(row.get("zero_thickness_probes", [])),
            planned=row.get("planned_zero_thickness_position_material_probes"),
            interior=row.get("interior_more_downhill_than_interface"),
            finite=row.get("finite_width_derivative_reversal_found"),
            exact=row.get("exact_score_rank_reversal_found"),
            evals=row.get("evaluation_count"),
        ))
    lines += ["", "A missing reversal is `not_found` within this frozen finite grid; it is not a claim that no reversal exists outside the sampled sites and widths.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_initial_baseline_report(results: Mapping[str, Any],
                                  path: Path = INITIAL_BASELINE_REPORT_PATH) -> None:
    lines = [
        "# Initial-stack baseline audit",
        "",
        f"Protocol: `{results.get('protocol_id', 'unknown')}`. These are frozen initial designs, scored without optimization.",
        "",
        "| Case | Training hard target | Fine holdout hard target | Max holdout violation | Layers | Total nm | Independent Airy max ΔR |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in results.get("runs", []):
        audit = row.get("layer_limits_audit", {})
        airy = row.get("independent_airy_check", {})
        lines.append("| {case} | {train} | {holdout} | {violation} | {layers} | {total} | {airy} |".format(
            case=row.get("case_id", ""), train=row.get("hard_target_met_training"),
            holdout=row.get("hard_target_met_holdout"),
            violation=_fmt(row.get("holdout_max_normalized_violation")),
            layers=audit.get("layer_count"),
            total=_fmt(audit.get("physical_total_thickness_nm")),
            airy=_fmt(airy.get("max_abs_R_delta")),
        ))
    lines += ["", "Each initial stack is held to the frozen exact physical target. This report is descriptive and is not an optimized-method result.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_report(results: Mapping[str, Any], path: Path = REPORT_PATH) -> None:
    lines = [
        "# Frozen film structure benchmark results",
        "",
        f"Protocol: `{results.get('protocol_id', 'unknown')}`. The physical targets and budgets were frozen before these runs.",
        "",
        "Synthetic lossless constant-index teaching cases only. Hard target attainment is recomputed on a separate fine holdout grid; 45° is a stress angle and is excluded from the target-met bit. Independent normal-incidence Airy checks do not call the project's optical kernel.",
        "",
        "| Case | Method | State / stop | Train hard target | Holdout hard target | Max holdout violation | Layers | Total nm | Objective evals | Jacobian evals | Candidates | Wall s | Airy max ΔR |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in results.get("runs", []):
        audit = row.get("layer_limits_audit") or {}
        airy = row.get("independent_airy_check") or {}
        holdout = row.get("holdout_max_normalized_violation")
        lines.append("| {case} | {method} | {state} / {stop} | {train} | {holdout_met} | {violation} | {layers} | {total} | {evals} | {jac} | {candidates} | {wall} | {airy} |".format(
            case=row.get("case_id", ""),
            method=row.get("method_id", ""),
            state=row.get("state", ""),
            stop=row.get("termination_reason", ""),
            train=row.get("hard_target_met_training", "—"),
            holdout_met=row.get("hard_target_met_holdout", "—"),
            violation=_fmt(row.get("holdout_max_normalized_violation")),
            layers=audit.get("layer_count", "—"),
            total=_fmt(audit.get("physical_total_thickness_nm")),
            evals=row.get("objective_evaluations", "—"),
            jac=row.get("jacobian_evaluations", "—"),
            candidates=row.get("structural_candidate_count", "—"),
            wall=_fmt(row.get("wall_clock_seconds")),
            airy=_fmt(airy.get("max_abs_R_delta")),
        ))
    lines += [
        "",
        "`True`/`False` in the hard-target columns uses the exact physical bounds from the frozen protocol. `algorithm_targets_met` is reported separately in the JSON because the optimizer accepts normalized error up to 0.001. Null counters mean that the method API did not expose that counter; they are not zero.",
        "",
        "Every failure, budget stop, unsupported search-time constraint, or non-improving structure proposal is retained in `benchmark_results.json`. No target or budget was adjusted after observing outcomes.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.6g}"
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                    encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and print the frozen case/method matrix without running optimization")
    parser.add_argument("--baseline-only", action="store_true",
                        help="audit only frozen initial stacks; do not invoke any optimizer")
    args = parser.parse_args(argv)
    protocol = load_protocol()
    if args.dry_run:
        summary = {
            "protocol_id": protocol["protocol_id"],
            "seed": protocol["reproducibility"]["seed"],
            "budget_per_method_per_case": protocol["solver_and_budget"]["budget_per_method_per_case"],
            "cases": [row["id"] for row in protocol["cases"]],
            "methods": [row["id"] for row in protocol["methods"]],
        }
        print(json.dumps(summary, indent=2))
        return 0
    if args.baseline_only:
        results = run_initial_baseline_audits(protocol)
        _write_json(INITIAL_BASELINE_PATH, results)
        write_initial_baseline_report(results)
        print(f"Wrote {INITIAL_BASELINE_PATH}")
        print(f"Wrote {INITIAL_BASELINE_REPORT_PATH}")
        return 0
    results = run_benchmarks(protocol)
    if not results.get("source_fingerprint_stable_for_entire_run"):
        _write_json(INVALIDATED_RESULTS_PATH, results)
        print(f"Source changed during this run; saved as invalidated evidence at {INVALIDATED_RESULTS_PATH}")
        return 2
    _write_json(RESULTS_PATH, results)
    write_report(results)
    print(f"Wrote {RESULTS_PATH}")
    print(f"Wrote {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
