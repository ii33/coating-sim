#!/usr/bin/env python3
"""Precision and synchronized throughput evidence for isolated CUDA TMM.

Run with --reference-only without installing Dr.Jit. CUDA/LLVM are opt-in
execution backends, never silent fallbacks. See docs/gpu-appearance-validation.md.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from coat_sim.core.film_optics import coherent_rta
from tools.validation.drjit_coherent_tmm import DrjitTmm, TmmBatch

# Fixed candidate validation gates, all explicitly PROJECT CONVENTIONS.
# Production passivity/balance values are copied from render.py _rta and must
# be reported separately from error versus the FP64 reference.
GATES = {
    "designation": "candidate gates, project convention; not externally certified",
    "max_absolute_error": {"float64": 2e-9, "float32": 2e-5},
    "existing_FP64_stack_equivalence": {"rtol": 2e-12, "atol": 2e-12,
                                         "origin": "current TorchCoherentBatch.verify project regression gate; separately reported"},
    "nonfinite_count": 0,
    "invalid_kernel_count": 0,
    "production_lower_bound": -1e-9,
    "production_upper_bound": 1 + 1e-8,
    "production_balance_absolute": 2e-7,
    "balance_is_constructed": "A=1-R-T; this detects numerical failures, not independent conservation validation",
}


def input_hash(batch: TmmBatch) -> str:
    digest = hashlib.sha256()
    for name in ("wavelength_nm", "indices", "thickness_nm", "beta"):
        array = np.ascontiguousarray(getattr(batch, name))
        digest.update(name.encode())
        digest.update(str(array.shape).encode())
        digest.update(array.dtype.str.encode())
        digest.update(array.tobytes())
    digest.update(batch.polarization.encode())
    return digest.hexdigest()


def reference(batch: TmmBatch) -> np.ndarray:
    # Production performs its own validation. Avoid an extra probe-side pass
    # here, particularly inside the fair NumPy batch timing region.
    rta = coherent_rta(batch.wavelength_nm, batch.indices, batch.thickness_nm,
                       beta=batch.beta, polarization=batch.polarization)
    return np.column_stack([rta[key] for key in ("R", "T", "A")])


def errors(actual: np.ndarray, expected: np.ndarray) -> dict:
    if actual.shape != expected.shape or actual.ndim != 2 or actual.shape[1] != 3:
        raise ValueError("expected matching [N,3] R/T/A arrays")
    output = {}
    for j, channel in enumerate(("R", "T", "A")):
        finite = np.isfinite(actual[:, j]) & np.isfinite(expected[:, j])
        delta = np.abs(actual[finite, j] - expected[finite, j])
        output[channel] = {
            "finite_compared": int(delta.size),
            "nonfinite_actual": int(np.count_nonzero(~np.isfinite(actual[:, j]))),
            "max_abs": float(delta.max()) if delta.size else None,
            "p99_abs": float(np.quantile(delta, .99)) if delta.size else None,
            "rms_abs": float(np.sqrt(np.mean(delta * delta))) if delta.size else None,
        }
    # Never compute relative errors for near-zero R/T/A.
    return output


def physics_checks(values: np.ndarray) -> dict:
    finite_rows = np.isfinite(values).all(axis=1)
    nonphysical = ((values < GATES["production_lower_bound"]) |
                   (values > GATES["production_upper_bound"]))
    balance = np.abs(np.sum(values, axis=1) - 1)
    return {
        "nonfinite_values": int(np.count_nonzero(~np.isfinite(values))),
        "nonfinite_rows": int(np.count_nonzero(~finite_rows)),
        "production_passivity_violating_values": int(np.count_nonzero(nonphysical)),
        "production_passivity_violating_rows": int(np.count_nonzero(nonphysical.any(axis=1))),
        "production_balance_violating_rows": int(np.count_nonzero(balance > GATES["production_balance_absolute"])),
        "max_balance_abs": float(balance[finite_rows].max()) if np.any(finite_rows) else None,
        "minimum_RTA": [float(values[finite_rows, j].min()) for j in range(3)] if np.any(finite_rows) else None,
        "maximum_RTA": [float(values[finite_rows, j].max()) for j in range(3)] if np.any(finite_rows) else None,
        "production_gate_pass": bool(finite_rows.all() and not nonphysical.any() and
                                     np.all(balance <= GATES["production_balance_absolute"])),
    }


def quantized_batch(batch: TmmBatch, precision: str) -> tuple[TmmBatch, np.ndarray]:
    dtype = np.float32 if precision == "float32" else np.float64
    n = batch.indices.real.astype(dtype).astype(np.float64) + 1j * batch.indices.imag.astype(dtype).astype(np.float64)
    b = batch.beta.astype(dtype).astype(np.float64)
    w = batch.wavelength_nm.astype(dtype).astype(np.float64)
    d = batch.thickness_nm.astype(dtype).astype(np.float64)
    legal = ((np.abs(b) < n[:, 0].real) & (n[:, 0].imag == 0) &
             np.isfinite(n).all(axis=1) & (n.real > 0).all(axis=1) &
             (n.imag >= 0).all(axis=1) & np.isfinite(b) & np.isfinite(w) & (w > 0))
    return replace(batch, wavelength_nm=w[legal], indices=n[legal], beta=b[legal], thickness_nm=d), legal


def compare(batch, actual, expected, precision, invalid, critical) -> dict:
    total_error = errors(actual, expected)
    quantized, legal = quantized_batch(batch, precision)
    quantized_reference = reference(quantized) if np.any(legal) else np.empty((0, 3))
    physical = physics_checks(actual)
    q_exit_reference = np.sqrt(batch.indices[:, -1] ** 2 - batch.beta ** 2 + 0j)
    exit_TIR = (batch.indices[:, -1].imag == 0) & (q_exit_reference.real == 0)
    physical["reference_zero_exit_flux_rows"] = int(np.count_nonzero(exit_TIR))
    physical["zero_exit_flux_nonzero_T_rows"] = int(np.count_nonzero(actual[exit_TIR, 1] != 0))
    physical["zero_exit_flux_gate_pass"] = not physical["zero_exit_flux_nonzero_T_rows"]
    max_errors = [item["max_abs"] for item in total_error.values()]
    accuracy_pass = (all(value is not None and value <= GATES["max_absolute_error"][precision] for value in max_errors)
                     and physical["nonfinite_values"] == 0 and not np.any(invalid))
    worst = []
    finite = np.isfinite(actual).all(axis=1)
    row_delta = np.max(np.abs(np.where(np.isfinite(actual), actual, 0) - expected), axis=1)
    for row in np.argsort(np.where(finite, row_delta, np.inf))[-5:][::-1]:
        worst.append({"row": int(row), "wavelength_nm": float(batch.wavelength_nm[row]),
                      "beta": float(batch.beta[row]),
                      "actual_RTA": [float(v) if np.isfinite(v) else None for v in actual[row]],
                      "reference_RTA": expected[row].tolist(),
                      "max_abs": float(row_delta[row]) if finite[row] else None})
    return {
        "canonical_FP64_inputs_total_error": total_error,
        "quantized_inputs_CPU_FP64_reference_arithmetic_error": errors(actual[legal], quantized_reference),
        "input_quantization_only_CPU_FP64_error": errors(quantized_reference, expected[legal]),
        "quantized_inputs_illegal_rows": int(np.count_nonzero(~legal)),
        "quantized_inputs_legal_rows": int(np.count_nonzero(legal)),
        "invalid_kernel_rows": int(np.count_nonzero(invalid)),
        "exact_critical_kernel_rows": int(np.count_nonzero(critical)),
        "physics": physical, "candidate_accuracy_gate_pass": bool(accuracy_pass),
        "existing_FP64_stack_equivalence_gate_pass": bool(np.isfinite(actual).all() and
            np.all(np.abs(actual - expected) <= 2e-12 + 2e-12 * np.abs(expected))),
        "candidate_gate_pass": bool(accuracy_pass and physical["production_gate_pass"] and physical["zero_exit_flux_gate_pass"] and np.all(legal)),
        "worst_rows": worst,
    }


def direction_quantization(batch: TmmBatch, expected: np.ndarray) -> dict:
    # beta/n0 fixes a physical angle. A real FP32 geometric wi.z is widened
    # BEFORE acos, matching production float(wi.z) -> double math.acos.
    ratio = batch.beta / batch.indices[:, 0].real
    cosine64 = np.sqrt(np.maximum(0, 1 - ratio * ratio))
    cosine32_then64 = cosine64.astype(np.float32).astype(np.float64)
    angle64 = np.minimum(np.rad2deg(np.arccos(cosine32_then64)), 89.999)
    beta = batch.indices[:, 0].real * np.sin(np.deg2rad(angle64))
    quantized = replace(batch, beta=beta)
    values = reference(quantized)
    return {
        "pipeline": "wi.z rounded FP32; widen FP64 before acos and 89.999deg cap; FP64 TMM",
        "max_cosine_input_abs": float(np.max(np.abs(cosine32_then64 - cosine64))),
        "max_beta_abs": float(np.max(np.abs(beta - batch.beta))),
        "RTA_absolute_error": errors(values, expected),
        "note": "separate geometric input quantization; not CUDA arithmetic error; cap included",
    }


def _batch(name, waves, indices, d, beta, **metadata):
    n = np.asarray(indices, dtype=np.complex128)
    w = np.asarray(waves, dtype=np.float64)
    if n.ndim == 1:
        n = np.tile(n, (w.size, 1))
    b = np.broadcast_to(np.asarray(beta, dtype=np.float64), w.shape).copy()
    return TmmBatch(name, w.copy(), n.copy(), np.asarray(d, dtype=np.float64), b,
                    metadata=metadata)


def _angle_batch(name, waves, indices, d, angles, **metadata):
    n = np.asarray(indices, dtype=np.complex128)
    if n.ndim == 1:
        n = np.tile(n, (len(waves), 1))
    a = np.asarray(angles, dtype=np.float64)
    nn = np.tile(n, (a.size, 1))
    ww = np.tile(waves, a.size)
    aa = np.repeat(a, len(waves))
    return _batch(name, ww, nn, d, nn[:, 0].real * np.sin(np.deg2rad(aa)),
                  angles_deg=a.tolist(), **metadata)


def synthetic_batches() -> list[TmmBatch]:
    waves = np.linspace(405, 830, 86)
    angles = [0, 30, 60, 80, 89, 89.999]
    cases = []
    for layers in (0, 60):
        jj = np.arange(layers)
        d = 35 + 2.5 * (jj % 11)
        for absorbing in (False, True) if layers else (False,):
            n = np.empty((len(waves), layers + 2), dtype=np.complex128)
            n[:, 0], n[:, -1] = 1, 1.47
            if layers:
                base = 1.55 + .35 * np.sin(jj * .37)
                dispersion = .015 * (550 / waves[:, None] - 1)
                k = .005 + .002 * (jj % 7) if absorbing else np.zeros(layers)
                n[:, 1:-1] = base[None, :] + dispersion + 1j * k[None, :]
            name = f"synthetic_{layers}layer_{'absorbing' if absorbing else 'lossless'}"
            cases.append(_angle_batch(name + "_forward", waves, n, d, angles, source="synthetic"))
            cases.append(_angle_batch(name + "_reverse", waves, n[:, ::-1], d[::-1], angles, source="synthetic", layer_order="reversed"))
    # Exact and neighboring q=0 internal layers, TIR/critical exit, and a
    # severely absorbing finite layer whose passive phase underflows safely.
    offsets = np.array([-1e-5, -1e-8, -1e-12, 0, 1e-12, 1e-8, 1e-5])
    ww = np.tile(waves, offsets.size)
    beta = np.repeat(1.25 + offsets, waves.size)
    n = np.full(62, 1.67 + .01j, dtype=np.complex128)
    n[0], n[-1], n[30] = 1.5, 1.6, 1.25
    d = 35 + 2.5 * (np.arange(60) % 11)
    cases.append(_batch("synthetic_60layer_internal_critical", ww, n, d, beta, source="synthetic", beta_offsets=offsets.tolist()))
    cases.append(_batch("synthetic_60layer_internal_critical_reverse", ww, n[::-1], d[::-1], beta, source="synthetic", layer_order="reversed"))
    cases.append(_batch("synthetic_adjacent_critical_exit_critical", waves, [1.5, 1, 1, 1], [25, 80], 1, source="synthetic"))
    cases.append(_batch("synthetic_zero_thickness_critical", waves, [1.5, 1, 1, 1.5], [0, 0], 1, source="synthetic"))
    cases.append(_angle_batch("synthetic_FP32_angle_neighbors_cross_TIR", waves, [1.5, 1], [],
                              [41.8103141784668, 41.810314895778596, 41.81031799316406],
                              source="synthetic", note="FP32 neighboring degree values straddle the 1.5->1 critical angle"))
    for label, offset in (("below", -1e-8), ("exact", 0), ("above", 1e-8)):
        cases.append(_batch("synthetic_zero_layer_exit_critical_" + label, waves,
                            [1.5, 1], [], 1 + offset, source="synthetic"))
    n = np.full(62, 1.72 + .04j, dtype=np.complex128)
    n[0], n[-1], n[12], n[43] = 1, 1.5 + .03j, .22 + 3.1j, .3 + 2.4j
    d = np.full(60, 65.)
    d[42] = 1e6
    cases.append(_angle_batch("synthetic_extreme_absorption_absorbing_exit", waves, n, d, [0, 51.137, 89.999], source="synthetic", reverse="not admissible: absorbing incident port forbidden"))
    # Direct beta nextafter isolates critical-layer limits from trig rounding.
    beta_ulps = [np.nextafter(1., 0.), 1., np.nextafter(1., np.inf)]
    for thickness in (0., 100., 270.):
        cases.append(_batch(f"synthetic_internal_critical_nextafter_d{thickness:g}",
                            np.tile(waves, 3), [1.5, 1, 1.5], [thickness],
                            np.repeat(beta_ulps, len(waves)), source="synthetic", beta_values=beta_ulps))
    brewster = np.rad2deg(np.arctan(1.5))
    theta32 = np.float32(brewster)
    cases.append(_angle_batch("synthetic_Brewster_degree_neighbors", waves, [1, 1.5], [],
                              [float(np.nextafter(theta32, np.float32(-np.inf))), brewster,
                               float(theta32), float(np.nextafter(theta32, np.float32(np.inf)))], source="synthetic"))
    n = np.r_[1., 1.55 + .35 * np.sin(np.arange(60) * .37) + .007j, 1.47]
    d = 35 + 2.5 * (np.arange(60) % 11)
    ww = np.tile(waves, 3)
    common_beta = np.repeat([0., .35, .7], len(waves))
    for direction, nn, dd in (("forward", n, d), ("reverse", n[::-1], d[::-1])):
        cases.append(_batch("synthetic_reciprocity_60layer_" + direction, ww, nn, dd,
                            common_beta, source="synthetic", reciprocity_group="synthetic60", direction=direction))
    # Each polarization is checked independently, then unpolarized averaging.
    return [replace(case, name=case.name + "_" + p, polarization=p)
            for case in cases for p in ("s", "p", "unpolarized")]


def appearance_batches(path: Path) -> tuple[list[TmmBatch], dict]:
    from coat_sim.appearance.contract import validate_job
    raw_bytes = path.read_bytes()
    job = json.loads(raw_bytes)
    # An explicitly invalid/enveloped file is rejected. No synthetic material
    # substitution or implicit schema guessing is permitted.
    data = validate_job(job)
    waves = data["wavelength_nm"]
    substrate = data["substrate"]["material"]
    ns = substrate["n_values"] + 1j * substrate["k_values"]
    cases, samples = [], []
    for face in ("front", "back"):
        layers = data[face + "_layers"]
        if layers:
            x, y = layers[0]["x_mm"], layers[0]["y_mm"]
            valid = np.logical_and.reduce([row["valid_mask"] for row in layers])
            if not np.any(valid):
                raise ValueError(f"{face}: no common valid captured thickness node")
            total = np.sum([np.where(valid, row["thickness_nm"], 0) for row in layers], axis=0)
            yy, xx = np.meshgrid(y, x, indexing="ij")
            center_x, center_y = (x[0] + x[-1]) / 2, (y[0] + y[-1]) / 2
            center_distance = (xx - center_x) ** 2 + (yy - center_y) ** 2
            choices = {
                "nearest_center_valid_node": np.argmin(np.where(valid, center_distance, np.inf)),
                "minimum_total_thickness_valid_node": np.argmin(np.where(valid, total, np.inf)),
                "maximum_total_thickness_valid_node": np.argmax(np.where(valid, total, -np.inf)),
            }
        else:
            choices = {"uncoated": None}
        for sample, flat in choices.items():
            if flat is not None:
                iy, ix = np.unravel_index(flat, valid.shape)
                d = np.array([row["thickness_nm"][iy, ix] for row in layers])
                location = {"x_mm": float(x[ix]), "y_mm": float(y[iy]), "ix": int(ix), "iy": int(iy)}
            else:
                d, location = np.empty(0), None
            spectral_n = [row["material"]["n_values"] + 1j * row["material"]["k_values"] for row in layers]
            # Frozen FilmDesign list is substrate -> outward. Exterior incidence
            # must reverse both finite layers and thickness, exactly as render.
            incident_orders = (
                ("outside_to_substrate", np.column_stack([np.ones(len(waves)), *spectral_n[::-1], ns]), d[::-1]),
                ("substrate_to_outside", np.column_stack([ns, *spectral_n, np.ones(len(waves))]), d),
            )
            samples.append({"face": face, "sample": sample, "location": location,
                            "layers": len(layers), "layer_ids_substrate_outward": [row["layer_id"] for row in layers],
                            "thickness_nm_substrate_outward": d.tolist(), "total_thickness_nm": float(d.sum())})
            for direction, n, depths in incident_orders:
                angles = np.array([0, 30, 60, 80, 89, 89.999])
                ordinary = _angle_batch(f"appearance_{face}_{sample}_{direction}", waves, n, depths, angles,
                                        source="validated appearance job", sample=sample, face=face,
                                        location=location, direction=direction)
                # Include substrate->air critical angle neighbors per wavelength,
                # and internal low-index critical boundaries when accessible.
                critical_candidates = n.real[:, 1:] / n.real[:, :1]
                critical_ratios = np.where((n.imag[:, 1:] == 0) & (critical_candidates < 1), critical_candidates, np.inf)
                minimum = np.min(critical_ratios, axis=1)
                accessible = np.isfinite(minimum)
                if np.any(accessible):
                    critical_beta = n[accessible, 0].real * minimum[accessible]
                    offsets = [-1e-10, 0, 1e-10]
                    critical = _batch(ordinary.name + "_critical_neighborhood",
                                      np.tile(waves[accessible], 3), np.tile(n[accessible], (3, 1)), depths,
                                      np.concatenate([critical_beta + offset for offset in offsets]),
                                      source="validated appearance job", beta_offsets=offsets, location=location)
                    for p in ("s", "p", "unpolarized"):
                        cases.append(replace(critical, name=critical.name + "_" + p, polarization=p))
                for p in ("s", "p", "unpolarized"):
                    cases.append(replace(ordinary, name=ordinary.name + "_" + p, polarization=p))
    return cases, {"path": str(path.resolve()), "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                   "wavelength_count": int(len(waves)), "wavelength_range_nm": [float(waves[0]), float(waves[-1])],
                   "sampling": "three explicitly sampled common valid captured grid nodes per coated face; no full field guarantee",
                   "samples": samples,
                   "material_inputs": {"substrate": job["substrate"]["material"],
                                       "front_substrate_outward": [row["material"] for row in job["front_layers"]],
                                       "back_substrate_outward": [row["material"] for row in job["substrate"]["back_surface"].get("layers", [])]},
                   "provenance": data.get("provenance", {})}


def resized_batch(batch: TmmBatch, size: int) -> TmmBatch:
    if size < 1:
        raise ValueError("batch size must be positive")
    row = np.arange(size) % batch.wavelength_nm.size
    return replace(batch, name=batch.name + f"_batch{size}", wavelength_nm=batch.wavelength_nm[row],
                   indices=batch.indices[row], beta=batch.beta[row])


def _distribution(seconds) -> dict:
    data = np.asarray(seconds)
    return {"seconds": data.tolist(), "median_seconds": float(np.median(data)),
            "min_seconds": float(data.min()), "max_seconds": float(data.max())}


@contextmanager
def capture_history(dr):
    """Dr.Jit 1.4 context capture, with its legacy dict interface supported."""
    history = dr.kernel_history()
    if hasattr(history, "__enter__"):
        with history as captured:
            yield captured
    else:
        with dr.scoped_set_flag(dr.JitFlag.KernelHistory, True):
            dr.kernel_history_clear()
            out = []
            yield out
            dr.sync_thread()
            out.extend(dr.kernel_history())


def serialize_history(history) -> list[dict]:
    out = []
    fields = ("backend", "type", "size", "input_count", "output_count", "hash",
              "operation_count", "codegen_time", "backend_time", "execution_time", "cache_hit", "cache_disk")
    for entry in history:
        row = {}
        for key in fields:
            try:
                value = entry.get(key) if isinstance(entry, dict) else getattr(entry, key)
            except (AttributeError, KeyError):
                continue
            if value is not None:
                row[key] = value if isinstance(value, (str, int, float, bool)) else str(value)
        try:
            source = entry.get("source") if isinstance(entry, dict) else entry.source
            row["source_sha256"] = hashlib.sha256(source.encode()).hexdigest() if source else None
            row["source_precision_tokens"] = {token: int(source.count(token)) for token in (".f64", ".f32", "double", "float")} if source else None
        except (AttributeError, TypeError):
            pass
        out.append(row)
    return out


def measure_device(runtime, batch, repeats) -> dict:
    dr = runtime.dr
    with dr.scoped_set_flag(dr.JitFlag.FastMath, False):
        dr.sync_thread()
        start = time.perf_counter()
        uploaded = runtime.upload(batch)
        dr.sync_thread()
        upload_seconds = time.perf_counter() - start
        # Flush only in-memory kernels; do not delete the user's disk cache.
        # History records whether the first evaluation was actually compiled.
        dr.flush_kernel_cache()
        dr.sync_thread()
        with capture_history(dr) as cold_history:
            start = time.perf_counter()
            out = runtime.solve_device(uploaded)
            dr.eval(out)
            dr.sync_thread()
            first_seconds = time.perf_counter() - start
            actual, invalid, critical = runtime.to_numpy(out)
            dr.sync_thread()
        cold = serialize_history(cold_history)
        del out
        timings, histories = [], []
        for _ in range(repeats):
            # No reference to earlier outputs survives. A freshly traced graph
            # must launch a kernel, not merely re-evaluate already cached data.
            dr.sync_thread()
            with capture_history(dr) as history:
                start = time.perf_counter()
                output = runtime.solve_device(uploaded)
                dr.eval(output)
                dr.sync_thread()
                timings.append(time.perf_counter() - start)
            histories.append(serialize_history(history))
            del output
        roundtrip = []
        for _ in range(repeats):
            dr.sync_thread()
            start = time.perf_counter()
            transient = runtime.upload(batch)
            output = runtime.solve_device(transient)
            dr.eval(output)
            dr.sync_thread()
            runtime.to_numpy(output)
            dr.sync_thread()
            roundtrip.append(time.perf_counter() - start)
            del output, transient
    def jit(entries):
        return [entry for entry in entries if "jit" in str(entry.get("type", "")).lower()]
    hot_launches = [jit(history) for history in histories]
    hot_execution_ms = [sum(float(row["execution_time"]) for row in rows if row.get("execution_time") is not None)
                        if rows and all(row.get("execution_time") is not None for row in rows) else None for rows in hot_launches]
    evidence = bool(jit(cold) and all(hot_launches) and
                    all(runtime.backend in str(row.get("backend", "")).lower() for rows in hot_launches for row in rows))
    return {
        "backend": runtime.backend, "precision": runtime.precision,
        "array_type": str(runtime.F), "complex_type": str(runtime.C), "FastMath": False,
        "input_sha256": input_hash(batch), "batch_size": int(batch.wavelength_nm.size),
        "layer_count": int(batch.thickness_nm.size), "upload_and_sync_seconds": upload_seconds,
        "first_trace_eval_sync_seconds_after_memory_cache_flush": first_seconds,
        "cold_JIT_history": cold,
        "cold_JIT_caveat": "disk cache retained; cache_hit/cache_disk/backend_time establish compile vs cached first use",
        "hot_resident_trace_eval_sync": _distribution(timings),
        "hot_upload_solve_download_sync": _distribution(roundtrip),
        "hot_JIT_execution_milliseconds": hot_execution_ms,
        "hot_JIT_history": histories,
        "actual_backend_kernel_launch_verified": evidence,
        "accuracy": compare(batch, actual, reference(batch), runtime.precision, invalid, critical),
    }


def _mitsuba_variant_in_child() -> tuple[object, dict]:
    """Probe the optional native runtime without loading it in the caller.

    Dr.Jit can terminate the interpreter during initialization (for example,
    when its default cache directory is read-only).  A native import is never
    safe in this report-only path, especially inside a large pytest process.
    """
    def stderr_text(value) -> str:
        return (value.decode("utf-8", errors="replace") if isinstance(value, bytes)
                else str(value or ""))[-2000:]

    with tempfile.TemporaryDirectory(prefix="tmm-mitsuba-probe-") as directory:
        home = Path(directory)
        for name in ("cache", "cuda"):
            (home / name).mkdir()
        env = os.environ.copy()
        env.update({"HOME": str(home), "USERPROFILE": str(home),
                    "XDG_CACHE_HOME": str(home / "cache"),
                    "CUDA_CACHE_PATH": str(home / "cuda"),
                    "PYTHONNOUSERSITE": "1"})
        code = 'import json, mitsuba as mi; print(json.dumps({"variant": mi.variant()}))'
        try:
            child = subprocess.run([sys.executable, "-I", "-B", "-c", code],
                                   cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   capture_output=True, timeout=30, check=False)
        except subprocess.TimeoutExpired as exc:
            return None, {"status": "timeout", "timeout_s": 30,
                          "stderr": stderr_text(exc.stderr)}
        except OSError as exc:
            return None, {"status": "launch_failed", "error": str(exc)[:500]}
    stderr = stderr_text(child.stderr)
    if child.returncode != 0:
        return None, {"status": "child_failed", "returncode": child.returncode,
                      "stderr": stderr}
    try:
        if len(child.stdout or b"") > 8192:
            raise ValueError("stdout exceeds 8192 bytes")
        payload = json.loads((child.stdout or b"").strip().splitlines()[-1].decode("utf-8"))
        if (not isinstance(payload, dict) or "variant" not in payload
                or not (payload["variant"] is None or isinstance(payload["variant"], str))):
            raise ValueError("variant payload is invalid")
    except (UnicodeError, ValueError, IndexError, KeyError) as exc:
        return None, {"status": "invalid_output", "returncode": child.returncode,
                      "error": str(exc)[:500], "stderr": stderr}
    return payload["variant"], {"status": "ok", "returncode": 0, "stderr": stderr}


def hardware(*, probe_variant: bool = True) -> dict:
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=name,uuid,driver_version,memory.total,compute_cap", "--format=csv,noheader"],
                                capture_output=True, text=True, check=False)
        gpu = {"returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    except OSError as exc:
        gpu = {"error": str(exc)}
    versions = {}
    for name in ("numpy", "drjit", "mitsuba"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    variant = None
    if not probe_variant:
        variant_probe = {"status": "not_requested_reference_only"}
    elif versions["mitsuba"]:
        try:
            variant, variant_probe = _mitsuba_variant_in_child()
        except OSError as exc:
            variant_probe = {"status": "probe_setup_failed", "error": str(exc)[:500]}
    else:
        variant_probe = {"status": "mitsuba_not_installed"}
    return {"HOME": os.environ.get("HOME"), "CUDA_CACHE_PATH": os.environ.get("CUDA_CACHE_PATH"), "platform": platform.platform(), "machine": platform.machine(),
            "processor": platform.processor(), "logical_cpu_count": os.cpu_count(),
            "python": sys.version, "versions": versions, "nvidia_smi": gpu,
            "mitsuba_variant": variant, "mitsuba_variant_probe": variant_probe,
            "variant_note": "Variant is read in an isolated child, not the TMM process; TMM uses explicit Dr.Jit types and does not change Mitsuba variant",
            "thread_environment": {key: os.environ.get(key) for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")}}


def run(args) -> dict:
    cases = synthetic_batches() if not args.no_synthetic else []
    appearance = None
    if args.appearance_job:
        real, appearance = appearance_batches(args.appearance_job)
        cases.extend(real)
    if args.case:
        cases = [case for case in cases if any(fragment in case.name for fragment in args.case)]
    if not cases:
        raise ValueError("no cases selected")
    report = {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "isolated coherent planar R/T/A precision and throughput; no image/BSDF/full-job speed claim",
              "hardware": hardware(probe_variant=not args.reference_only),
              "gates": GATES, "appearance_job": appearance,
              "source_hashes": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in
                                (Path(__file__), ROOT / "tools/validation/drjit_coherent_tmm.py", ROOT / "coat_sim/core/film_optics.py")},
              "cases": [], "benchmarks": [], "backend_failures": [],
              "coverage_limits": ["Z6 dense narrow-resonance center/FWHM localization is not covered; coarse 86-wave samples cannot qualify it",
                                  "no complete captured spatial-field bound; appearance job samples are explicit selected nodes",
                                  "no BSDF/ray transport/image agreement or full-render speed measured"],
              "invariant_comparisons": []}
    runtimes = []
    if not args.reference_only:
        for backend in args.backends:
            for precision in args.precisions:
                try:
                    runtime = DrjitTmm(backend, precision)
                    if args.llvm_threads is not None:
                        runtime.dr.set_thread_count(args.llvm_threads)
                    runtimes.append(runtime)
                except Exception as exc:
                    report["backend_failures"].append({"backend": backend, "precision": precision, "error": repr(exc)})
    solved, raw_arrays, raw_index = {}, {}, []
    for case_index, batch in enumerate(cases):
        batch.validate()
        expected = reference(batch)
        prefix = f"case_{case_index:03d}"
        raw_arrays[prefix + "_CPU_FP64_RTA"] = expected
        for input_name in ("wavelength_nm", "indices", "thickness_nm", "beta"):
            raw_arrays[prefix + "_input_" + input_name] = getattr(batch, input_name)
        raw_index.append({"prefix": prefix, "name": batch.name, "input_sha256": input_hash(batch)})
        item = {"name": batch.name, "input_sha256": input_hash(batch), "batch_size": int(len(batch.wavelength_nm)),
                "layer_count": int(len(batch.thickness_nm)), "polarization": batch.polarization,
                "metadata": batch.metadata, "CPU_FP64_reference_physics": physics_checks(expected),
                "direction_input_quantization": direction_quantization(batch, expected), "results": []}
        for runtime in runtimes:
            try:
                runtime.dr.sync_thread()
                with capture_history(runtime.dr) as history:
                    start = time.perf_counter()
                    actual, invalid, critical = runtime.evaluate(batch)
                    runtime.dr.sync_thread()
                    case_seconds = time.perf_counter() - start
                key = prefix + "_" + runtime.backend + "_" + runtime.precision
                raw_arrays[key + "_RTA"] = actual
                raw_arrays[key + "_invalid"] = invalid
                raw_arrays[key + "_critical"] = critical
                solved[(batch.name, runtime.backend, runtime.precision)] = actual
                launches = serialize_history(history)
                jit = [entry for entry in launches if "jit" in str(entry.get("type", "")).lower()]
                verified = bool(jit and all(runtime.backend in str(entry.get("backend", "")).lower() for entry in jit))
                item["results"].append({"backend": runtime.backend, "precision": runtime.precision,
                                        "FastMath": False, "thread_count": int(runtime.dr.thread_count()),
                                        "pi_constant": {"canonical_FP64": float(np.pi),
                                                        "stored_precision": float(runtime.numpy_float(np.pi)),
                                                        "canonical_tau": float(2 * np.pi),
                                                        "stored_tau": float(runtime.numpy_float(2 * np.pi))},
                                        "raw_RTA_array_key": key + "_RTA",
                                        "first_case_upload_trace_eval_download_sync_seconds": case_seconds,
                                        "kernel_history": launches, "actual_backend_kernel_launch_verified": verified,
                                        "cold_compile_observed": any(float(entry.get("backend_time", 0)) > 0 and not entry.get("cache_hit", False) for entry in jit),
                                        "accuracy": compare(batch, actual, expected, runtime.precision, invalid, critical)})
            except Exception as exc:
                item["results"].append({"backend": runtime.backend, "precision": runtime.precision, "error": repr(exc)})
        report["cases"].append(item)
    by_name = {batch.name: batch for batch in cases}
    for batch in cases:
        metadata = batch.metadata or {}
        if metadata.get("reciprocity_group") and metadata.get("direction") == "forward":
            reverse_name = batch.name.replace("_forward_", "_reverse_")
            reverse = by_name.get(reverse_name)
            if reverse is not None:
                values, back = reference(batch), reference(reverse)
                report["invariant_comparisons"].append({"invariant": "same-real-beta power transmission reciprocity",
                    "forward": batch.name, "reverse": reverse.name,
                    "CPU_FP64_max_abs_T_difference": float(np.max(np.abs(values[:, 1] - back[:, 1]))),
                    "JIT_results": [{"backend": rt.backend, "precision": rt.precision,
                                     "max_abs_T_difference": float(np.max(np.abs(solved[(batch.name, rt.backend, rt.precision)][:, 1] - solved[(reverse.name, rt.backend, rt.precision)][:, 1]))) if
                                     np.isfinite(solved[(batch.name, rt.backend, rt.precision)]).all() and np.isfinite(solved[(reverse.name, rt.backend, rt.precision)]).all() else None}
                                    for rt in runtimes if (batch.name, rt.backend, rt.precision) in solved and (reverse.name, rt.backend, rt.precision) in solved]})
        if batch.polarization == "unpolarized":
            prefix = batch.name.removesuffix("_unpolarized")
            s_case, p_case = by_name.get(prefix + "_s"), by_name.get(prefix + "_p")
            if s_case is not None and p_case is not None:
                average = (reference(s_case) + reference(p_case)) / 2
                report["invariant_comparisons"].append({"invariant": "unpolarized=(s+p)/2 power average", "case": batch.name,
                    "CPU_FP64_absolute_error": errors(reference(batch), average),
                    "JIT_results": [{"backend": rt.backend, "precision": rt.precision,
                                     "absolute_error": errors(solved[(batch.name, rt.backend, rt.precision)],
                                          (solved[(s_case.name, rt.backend, rt.precision)] + solved[(p_case.name, rt.backend, rt.precision)]) / 2)}
                                    for rt in runtimes if all((name, rt.backend, rt.precision) in solved for name in (batch.name, s_case.name, p_case.name))]})
    if not args.no_benchmark:
        # Benchmark regular 0/60-layer unpolarized cases, excluding grazing and
        # exact critical fallback rows to avoid a CPU scalar-fallback speedup.
        benchmark_candidates = [case for case in cases if case.polarization == "unpolarized" and
                                len(case.thickness_nm) in (0, 60) and
                                "critical" not in case.name and "reverse" not in case.name and
                                (case.metadata or {}).get("direction") != "substrate_to_outside"]
        chosen = {}
        for case in benchmark_candidates:
            layers = len(case.thickness_nm)
            previous = chosen.get(layers)
            if previous is None or ((case.metadata or {}).get("source") == "validated appearance job" and
                                    (previous.metadata or {}).get("source") != "validated appearance job"):
                chosen[layers] = case
        for base in chosen.values():
            regular = np.abs(base.beta / base.indices[:, 0].real) <= np.sin(np.deg2rad(80.))
            base = replace(base, wavelength_nm=base.wavelength_nm[regular], indices=base.indices[regular], beta=base.beta[regular])
            for size in args.batch_sizes:
                batch = resized_batch(base, size)
                reference(batch)  # CPU warm-up excluded from timing.
                cpu_seconds = []
                for _ in range(args.repeats):
                    start = time.perf_counter()
                    values = reference(batch)
                    cpu_seconds.append(time.perf_counter() - start)
                    del values
                row = {"name": batch.name, "input_sha256": input_hash(batch), "batch_size": size,
                       "layer_count": len(batch.thickness_nm), "metadata": batch.metadata,
                       "CPU_production_NumPy_batch_FP64": _distribution(cpu_seconds),
                       "comparison_scope": "same batch data and stable physics; production NumPy batch vs explicitly typed JIT implementations",
                       "results": []}
                for runtime in runtimes:
                    try:
                        measured = measure_device(runtime, batch, args.repeats)
                        measured["production_batch_vs_resident_implementation_throughput_ratio"] = float(np.median(cpu_seconds) / measured["hot_resident_trace_eval_sync"]["median_seconds"])
                        measured["production_batch_vs_roundtrip_implementation_throughput_ratio"] = float(np.median(cpu_seconds) / measured["hot_upload_solve_download_sync"]["median_seconds"])
                        row["results"].append(measured)
                    except Exception as exc:
                        row["results"].append({"backend": runtime.backend, "precision": runtime.precision, "error": repr(exc)})
                # Same-precision LLVM/CUDA graph device timers isolate backend
                # compute more fairly than NumPy/Python wall-clock comparisons.
                for gpu in row["results"]:
                    if gpu.get("backend") != "cuda" or "hot_JIT_execution_milliseconds" not in gpu:
                        continue
                    cpu = next((item for item in row["results"] if item.get("backend") == "llvm" and item.get("precision") == gpu["precision"] and "hot_JIT_execution_milliseconds" in item), None)
                    valid_times = gpu["hot_JIT_execution_milliseconds"]
                    if cpu and all(v is not None and v > 0 for v in valid_times + cpu["hot_JIT_execution_milliseconds"]):
                        gpu["same_graph_same_precision_LLVM_over_CUDA_device_timer_ratio"] = float(np.median(cpu["hot_JIT_execution_milliseconds"]) / np.median(valid_times))
                report["benchmarks"].append(row)
    expected_pairs = {(backend, precision) for backend in args.backends for precision in args.precisions}
    report["requested_backend_precision_pairs"] = [list(item) for item in sorted(expected_pairs)] if not args.reference_only else []
    results = [row for item in report["cases"] for row in item["results"]]
    report["summary"] = {
        "reference_only": args.reference_only,
        "case_count": len(cases), "evaluated_result_count": len(results),
        "CPU_FP64_reference_production_gate_failures": sum(not item["CPU_FP64_reference_physics"]["production_gate_pass"] for item in report["cases"]),
        "errors": len(report["backend_failures"]) + sum("error" in row for row in results) + sum("error" in row for item in report["benchmarks"] for row in item["results"]),
        "kernel_launch_evidence_failures": sum(not row.get("actual_backend_kernel_launch_verified", False) for row in results if "accuracy" in row),
        "accuracy_gate_failures": sum(not row["accuracy"]["candidate_accuracy_gate_pass"] for row in results if "accuracy" in row),
        "production_gate_failures": sum(not row["accuracy"]["physics"]["production_gate_pass"] for row in results if "accuracy" in row),
        "candidate_gate_failures": sum(not row["accuracy"]["candidate_gate_pass"] for row in results if "accuracy" in row),
        "GPU_verification_status": "not run" if args.reference_only else "inspect per-precision gates and actual backend kernel histories; no global precision approval implied",
    }
    if getattr(args, "output", None) is not None:
        raw_path = args.output.with_suffix(".raw.npz")
        if raw_path.exists():
            raise FileExistsError(f"raw output already exists: {raw_path}")
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(raw_path, **raw_arrays)
        report["raw_results"] = {"path": str(raw_path.resolve()),
                                 "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                                 "arrays": raw_index, "note": "original R/T/A, no passivity clipping or renormalization; NaN retained if rejected"}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--appearance-job", type=Path)
    parser.add_argument("--no-synthetic", action="store_true")
    parser.add_argument("--case", action="append", help="select cases containing a fragment; repeat for OR")
    parser.add_argument("--backends", nargs="+", choices=("cuda", "llvm"), default=["cuda", "llvm"])
    parser.add_argument("--precisions", nargs="+", choices=("float64", "float32"), default=["float64", "float32"])
    parser.add_argument("--batch-sizes", nargs="+", type=int, default=[256, 8192, 65536])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--llvm-threads", type=int, help="explicit CPU threads; omit to retain runtime default")
    parser.add_argument("--reference-only", action="store_true")
    parser.add_argument("--no-benchmark", action="store_true")
    args = parser.parse_args()
    if args.repeats < 1 or any(size < 1 for size in args.batch_sizes) or (args.llvm_threads is not None and args.llvm_threads < 1):
        parser.error("repeats, batch sizes and explicit LLVM thread count must be positive")
    if args.output.exists():
        parser.error("output already exists; choose a fresh path")
    report = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "summary": report["summary"]}, ensure_ascii=False))
    # A precision rejection is valid evidence, not an execution failure. It is
    # preserved in JSON. Runtime/schema/launch failure is nonzero exit status.
    return 2 if report["summary"]["errors"] or report["summary"]["kernel_launch_evidence_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
