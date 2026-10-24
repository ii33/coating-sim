"""Finite real-optics benchmark, no UI/catalog/process-setting mutations.

Run from the repository root with .conda/python.exe; writes only the requested
report directory. Three repeats, at most four CPU workers; CUDA is opt-in.
"""
from __future__ import annotations


import argparse
from copy import deepcopy
from dataclasses import replace
import ctypes
import json
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from coat_sim.core.film_performance import (FrozenFilmEvaluator, PerformanceOptions,
    KERNEL_VERSION, environment_key, optimize_film_design_accelerated)
from coat_sim.core.film_optimization import (FilmObjectiveConstraint, FilmOptimizationRequest, FilmOptimizationObjective,
    FilmThicknessVariable, optimize_film_design)
from coat_sim.core.film_optimization_derivatives import simulate_with_thickness_jacobian
from coat_sim.core.film_optics import coherent_rta
from tests.film_performance_fixtures import fixture

def peak_ram():
    if os.name != "nt":
        return None
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (key, ctypes.c_size_t) for key in ("peak_ws", "ws", "peak_paged", "paged",
                                              "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]
    info = Counters(); info.cb = ctypes.sizeof(info)
    ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    if ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(ctypes.windll.kernel32.GetCurrentProcess()),
                                               ctypes.byref(info), info.cb):
        return int(info.peak_ws)
    return None

def timed(call):
    begin = time.perf_counter(); value = call()
    return time.perf_counter() - begin, value

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--gpu", action="store_true", help="explicitly test existing CUDA after bounded probe")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"kernel_version": KERNEL_VERSION, "environment_key": environment_key(),
              "numpy": np.__version__, "python": sys.version, "logical_cpus": os.cpu_count(),
              "repetitions": 3, "precision": "float64/complex128", "groups": [],
              "cuda": {"tested": False, "reason": "GPU test not requested; availability not inferred"}}
    profile = {"kernel_version": KERNEL_VERSION, "environment_key": environment_key(), "regions": []}
    start_all = time.perf_counter()
    for label, candidates, waves, layers in [("small", 8, 31, 4), ("medium", 12, 1001, 12),
                                             ("large", 8, 50001, 18)]:
        print(f"Computing {label}: {candidates} candidates x {waves} wavelengths x {layers} layers", flush=True)
        service, design, frozen = fixture(layers=layers)
        settings = dict(wavelength_min_nm=400., wavelength_max_nm=800., samples=waves,
                        angle_deg=37., polarization="unpolarized")
        ids = [row["id"] for row in design["front_layers"]]
        initial = np.array([row["thickness"]["value"] for row in design["front_layers"]])
        d = initial[None, :] * np.random.default_rng(17).uniform(.8, 1.2, (candidates, layers))
        def baseline():
            out = np.empty((candidates, waves, 3))
            for i, row in enumerate(d):
                candidate = deepcopy(design)
                for layer, value in zip(candidate["front_layers"], row):
                    layer["thickness"] = {"value": float(value), "unit": "nm"}
                response = service.simulate(candidate, settings)
                out[i] = np.column_stack([response[key] for key in ("R", "T", "A")])
            return out
        cold_baseline, expected = timed(baseline)
        baseline_repeats = [timed(baseline)[0] for _ in range(3)]
        group = {"name": label, "candidates": candidates, "waves": waves, "layers": layers,
                 "cold_service_batch_seconds": cold_baseline,
                 "service_repeats_seconds": baseline_repeats,
                 "service_median_seconds": statistics.median(baseline_repeats), "variants": []}
        for workers in (1, 2, 4):
            prepare_time, evaluator = timed(lambda: FrozenFilmEvaluator(frozen, settings,
                options=PerformanceOptions(backend="cpu", cpu_workers=workers)))
            def forward():
                response = evaluator.evaluate_thickness_batch(ids, d)
                return np.stack([response[key] for key in ("R", "T", "A")], axis=2)
            cold_forward, actual = timed(forward)
            error = float(np.max(np.abs(expected - actual)))
            np.testing.assert_allclose(actual, expected, rtol=2e-12, atol=2e-12)
            repeats = [timed(forward)[0] for _ in range(3)]
            median = statistics.median(repeats)
            group["variants"].append({"backend": "cpu", "workers": workers,
                "prepare_seconds": prepare_time, "first_forward_seconds": cold_forward,
                "warm_repeats_seconds": repeats, "warm_median_seconds": median,
                "end_to_end_first_seconds": prepare_time + cold_forward,
                "speedup_vs_service_warm": group["service_median_seconds"] / median,
                "max_abs_error": error, "peak_process_ram_bytes": peak_ram()})
        stack = evaluator._batch.stack
        # Separate the old NumPy kernel from full service reporting/preparation.
        ordered_d = d[:, ::-1]
        def raw_reference():
            for row in ordered_d:
                coherent_rta(stack.waves, stack.indices, row, angle_deg=37., polarization="unpolarized")
        group["old_numpy_kernel_seconds"] = [timed(raw_reference)[0] for _ in range(3)]
        if args.gpu:
            evaluator = FrozenFilmEvaluator(frozen, settings, options=PerformanceOptions(backend="gpu"))
            gpu_time, actual = timed(lambda: evaluator.evaluate_thickness_batch(ids, d))
            if actual["metadata"]["backend"] == "gpu":
                error = max(float(np.max(np.abs(actual[key] - expected[:, :, i]))) for i, key in enumerate(("R", "T", "A")))
                verified = all(np.allclose(actual[key], expected[:, :, i], rtol=2e-12, atol=2e-12)
                               for i, key in enumerate(("R", "T", "A")))
                repeats = []
                if verified:
                    for _ in range(3):
                        seconds, repeated = timed(lambda: evaluator.evaluate_thickness_batch(ids, d))
                        repeats.append(seconds)
                        verified = verified and repeated["metadata"]["backend"] == "gpu" and all(
                            np.allclose(repeated[key], expected[:, :, i], rtol=2e-12, atol=2e-12)
                            for i, key in enumerate(("R", "T", "A")))
                report["cuda"] = {"tested": True, "passed": report["cuda"].get("passed", True) and verified,
                                  "device": actual["metadata"]["device"]}
                group["gpu"] = {"passed": verified, "first_total_seconds": gpu_time,
                                 "repeats_seconds": repeats, "max_abs_error": error,
                                 "metadata": actual["metadata"]}
            else:
                report["cuda"] = {"tested": report["cuda"].get("tested", False), "passed": False,
                                  "reason": actual["metadata"].get("fallback_reason")}
                group["gpu"] = {"passed": False, "fallback": actual["metadata"]}
        variables = [FilmThicknessVariable("front", key, 20., 200.) for key in ids[:2]]
        gradient_times = []
        def gradient_full():
            # Respect the existing analytic-J service budget without changing
            # its domain or dropping any full-grid wavelength.
            full_waves = np.linspace(settings["wavelength_min_nm"], settings["wavelength_max_nm"], waves)
            chunks = []
            for start in range(0, waves, 10001):
                tile = full_waves[start:start + 10001]
                chunks.append(simulate_with_thickness_jacobian(design, frozen, variables,
                    dict(settings, wavelength_min_nm=float(tile[0]), wavelength_max_nm=float(tile[-1]), samples=len(tile))))
            return np.concatenate([chunk["jacobian_R"] for chunk in chunks])
        for _ in range(3):
            seconds, gradient = timed(gradient_full)
            gradient_times.append(seconds)
        group["gradient_cpu_seconds"] = gradient_times
        group["gradient_variable_count"] = len(variables)
        group["gradient_chunks"] = (waves + 10000) // 10001
        group["gradient_gpu"] = "not implemented; CPU analytic J"
        # Do not generalize a threshold from a synthetic dimension to other
        # unmeasured grids/layer counts. Retain serial unless >=20% faster.
        serial = group["variants"][0]["warm_median_seconds"]
        best = min(group["variants"], key=lambda row: row["warm_median_seconds"])
        selected_workers = best["workers"] if best["warm_median_seconds"] <= serial * .8 else 1
        profile["regions"].append({"backend": "cpu", "cpu_workers": selected_workers,
            "min_candidates": candidates, "max_candidates": candidates,
            "min_waves": waves, "max_waves": waves, "layers": layers,
            "polarization": "unpolarized", "chunk_bytes": 64 * 1024**2,
            "repetitions": 3, "verified": True})
        group["automatic_workers_for_measured_region"] = selected_workers
        report["groups"].append(group)
        (output / "benchmark-in-progress.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    service, design, frozen = fixture(layers=4)
    request = FilmOptimizationRequest(design, 1,
        tuple(FilmThicknessVariable("front", f"layer-{i}", 25., 160.) for i in range(2)),
        (FilmOptimizationObjective(450., 700., 301, pointwise=FilmObjectiveConstraint(mode="value", target_value=.05, lower_bound=None, upper_bound=None)),),
        solver="differential_evolution", max_iterations=3, max_evaluations=49,
        seed=19, population_size=12, frozen_material_snapshot=frozen,
        acceptable_normalized_error=0., validation_samples_multiplier=1)
    base_seconds, base = timed(lambda: optimize_film_design(service, request))
    fast_seconds, fast = timed(lambda: optimize_film_design_accelerated(service, request))
    assert base.evaluations == fast.evaluations
    assert abs(base.max_normalized_error - fast.max_normalized_error) < 2e-12
    base_warm, fast_warm = [], []
    for _ in range(3):
        seconds, reference = timed(lambda: optimize_film_design(service, request))
        base_warm.append(seconds)
        seconds, accelerated = timed(lambda: optimize_film_design_accelerated(service, request))
        fast_warm.append(seconds)
        assert reference.evaluations == accelerated.evaluations == base.evaluations
        assert abs(reference.max_normalized_error - accelerated.max_normalized_error) < 2e-12
    report["existing_immediate_de_end_to_end"] = {"first_service_seconds_including_scipy_import": base_seconds,
        "first_cached_cpu_seconds_after_scipy_loaded": fast_seconds,
        "service_warm_seconds": base_warm, "cached_cpu_warm_seconds": fast_warm,
        "evaluations_each": base.evaluations,
        "max_error_base": base.max_normalized_error, "max_error_fast": fast.max_normalized_error,
        "warm_speedup": statistics.median(base_warm) / statistics.median(fast_warm),
        "mode": "existing-immediate-unchanged"}
    report["total_benchmark_seconds"] = time.perf_counter() - start_all
    report["peak_process_ram_bytes"] = peak_ram()
    (output / "benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (output / "calibration.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)

if __name__ == "__main__":
    main()
