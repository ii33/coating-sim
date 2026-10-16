#!/usr/bin/env python3
"""Compare the isolated TMM equations with an independent 100-digit oracle.

The NumPy backend is an equation adapter, never evidence of CUDA execution.
The JSON proof and adjacent NPZ preserve exact input arrays and raw results.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.validation.drjit_coherent_tmm import DrjitTmm
from tools.validation.drjit_tmm_stable_candidate import StableDrjitTmm
from tools.validation.gpu_tmm_probe import (
    _batch, capture_history, errors, input_hash, physics_checks, reference,
    serialize_history,
)
from tools.validation.tmm_boundary_oracle import ORACLE_DECIMAL_DIGITS, oracle_rta


ORACLE_PATH = ROOT / "tools/validation/tmm_boundary_oracle.py"
ORACLE_SHA256 = hashlib.sha256(ORACLE_PATH.read_bytes()).hexdigest()
POLARIZATIONS = ("s", "p", "unpolarized")
WAVELENGTHS_NM = (405., 550., 830.)


def boundary_batches():
    """Fifteen physical groups, each exercised in all three polarizations."""
    neighbors = (("below", np.nextafter(1., 0.)), ("exact", 1.),
                 ("above", np.nextafter(1., np.inf)))
    groups = []
    for thickness in (0., 100., 270.):
        for label, beta in neighbors:
            groups.append(_batch(f"internal_nextafter_d{thickness:g}_{label}",
                                 WAVELENGTHS_NM, [1.5, 1., 1.5], [thickness], beta))
    for label, beta in neighbors:
        groups.append(_batch(f"absorbing_critical_thick_nextafter_{label}",
                             WAVELENGTHS_NM, [1.5, 1., .22 + 3.1j, 1.5],
                             [100., 1e6], beta))
    for label, beta in neighbors:
        groups.append(_batch(f"bare_exit_nextafter_{label}",
                             WAVELENGTHS_NM, [1.5, 1.], [], beta))
    return [replace(group, name=f"{group.name}_{polarization}",
                    polarization=polarization)
            for group in groups for polarization in POLARIZATIONS]


class _NumpyDr:
    """Only the arithmetic vocabulary of the isolated equations."""
    class JitFlag:
        FastMath = "FastMath"

    select = staticmethod(np.where)
    sqrt = staticmethod(np.sqrt)
    exp = staticmethod(np.exp)
    cos = staticmethod(np.cos)
    conj = staticmethod(np.conj)
    abs = staticmethod(np.abs)
    isfinite = staticmethod(np.isfinite)
    maximum = staticmethod(np.maximum)

    @staticmethod
    def flag(_flag):
        return False

    @staticmethod
    def uint32_array_t(_float_type):
        return lambda value: np.asarray(value, dtype=np.uint32)


def numpy_equations(batch, candidate: bool):
    """Evaluate equations in binary64 NumPy, without pretending to run a GPU."""
    cls = StableDrjitTmm if candidate else DrjitTmm
    runtime = object.__new__(cls)
    runtime.dr = _NumpyDr()
    runtime.C = lambda real=0, imag=0: (
        np.asarray(real, dtype=np.complex128) + 1j * np.asarray(imag, dtype=np.float64))
    runtime.F = lambda value: np.asarray(value, dtype=np.float64)
    runtime.B = lambda value: np.full(batch.wavelength_nm.shape, value, dtype=bool)
    if candidate:
        keep = batch.thickness_nm != 0
        columns = np.r_[0, np.flatnonzero(keep) + 1, batch.indices.shape[1] - 1]
        batch = replace(batch, indices=batch.indices[:, columns],
                        thickness_nm=batch.thickness_nm[keep])
    uploaded = {"w": batch.wavelength_nm,
                "n": tuple(batch.indices[:, j] for j in range(batch.indices.shape[1])),
                "d": tuple(batch.thickness_nm), "beta": batch.beta,
                "polarization": batch.polarization}
    with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
        output = runtime.solve_device(uploaded)
    return (np.column_stack(output[:3]), np.asarray(output[3], dtype=bool),
            np.asarray(output[4], dtype=bool),
            np.asarray(output[5], dtype=bool) if candidate else None,
            np.asarray(output[6], dtype=np.uint32) if candidate else None)


def _device_equations(batch, backend: str, precision: str, candidate: bool):
    runtime = (StableDrjitTmm if candidate else DrjitTmm)(backend, precision)
    dr = runtime.dr
    with dr.scoped_set_flag(dr.JitFlag.FastMath, False):
        uploaded = runtime.upload(batch)
        dr.sync_thread()
        with capture_history(dr) as history:
            output = runtime.solve_device(uploaded)
            dr.eval(output)
            dr.sync_thread()
        values = np.column_stack([np.asarray(v, dtype=np.float64) for v in output[:3]])
        invalid = np.asarray(output[3], dtype=bool)
        exact = np.asarray(output[4], dtype=bool)
        near = np.asarray(output[5], dtype=bool) if candidate else None
        series = np.asarray(output[6], dtype=np.uint32) if candidate else None
    launches = serialize_history(history)
    jit = [row for row in launches if "jit" in str(row.get("type", "")).lower()]
    return (values, invalid, exact, near, series), launches, bool(
        jit and all(backend in str(row.get("backend", "")).lower() for row in jit))


def _fixture_values(path: Path, batches) -> dict[str, np.ndarray]:
    saved = json.loads(path.read_text(encoding="utf-8"))
    oracle = saved.get("oracle", {})
    if (oracle.get("source_sha256") != ORACLE_SHA256 or
            oracle.get("decimal_digits", 0) < ORACLE_DECIMAL_DIGITS):
        raise ValueError("oracle fixture source SHA256 or 100-digit marker differs")
    items = {item["name"]: item for item in saved.get("cases", [])}
    values = {}
    for batch in batches:
        item = items.get(batch.name)
        if item is None or item.get("input_sha256") != input_hash(batch):
            raise ValueError(f"oracle fixture input hash differs: {batch.name}")
        shape = [len(batch.wavelength_nm), 3]
        if item.get("shape") != shape:
            raise ValueError(f"oracle fixture shape differs: {batch.name}")
        array = np.asarray(item.get("oracle_RTA"), dtype=np.float64)
        if list(array.shape) != shape or not np.isfinite(array).all():
            raise ValueError(f"oracle fixture RTA invalid: {batch.name}")
        values[batch.name] = array
    return values


def run(*, backend="numpy", precision="float64", cases=None,
        oracle_fixture=None, output=None):
    if backend not in ("numpy", "cuda", "llvm"):
        raise ValueError("backend must be numpy, cuda or llvm")
    if precision not in ("float64", "float32"):
        raise ValueError("precision must be float64 or float32")
    if backend == "numpy" and precision != "float64":
        raise ValueError("NumPy equation adapter is binary64 only")
    batches = boundary_batches()
    if cases:
        batches = [batch for batch in batches if any(term in batch.name for term in cases)]
    if not batches:
        raise ValueError("no boundary cases selected")
    fixtures = _fixture_values(Path(oracle_fixture), batches) if oracle_fixture else {}
    report = {"schema_version": 1,
              "created_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "isolated boundary validation; not production or full GPU BSDF certification",
              "backend": backend, "precision": precision,
              "numpy_is_equation_adapter_only": backend == "numpy",
              "oracle": {"method": "independent analytic E/H field propagation using mpmath",
                         "decimal_digits": ORACLE_DECIMAL_DIGITS,
                         "source_sha256": ORACLE_SHA256,
                         "fixture": str(oracle_fixture) if oracle_fixture else None},
              "candidate_max_abs_gate": 2e-12 if precision == "float64" else 2e-5,
              "cases": []}
    arrays = {}
    original_failures = candidate_failures = 0
    for index, batch in enumerate(batches):
        key = f"case_{index:03d}"
        expected = fixtures.get(batch.name)
        if expected is None:
            expected = oracle_rta(batch)
        cpu = reference(batch)
        raw = {}
        for label, candidate in (("original", False), ("candidate", True)):
            if backend == "numpy":
                result = numpy_equations(batch, candidate)
                history, verified = [], False
            else:
                result, history, verified = _device_equations(batch, backend, precision, candidate)
            actual, invalid, exact, near, series = result
            difference = float(np.max(np.abs(actual - expected))) if np.isfinite(actual).all() else None
            physics = physics_checks(actual)
            passed = bool(difference is not None and
                          difference <= report["candidate_max_abs_gate"] and
                          not invalid.any() and physics["production_gate_pass"])
            if not passed:
                if candidate:
                    candidate_failures += 1
                else:
                    original_failures += 1
            arrays[key + "_" + label + "_RTA"] = actual
            arrays[key + "_" + label + "_invalid"] = invalid
            arrays[key + "_" + label + "_exact"] = exact
            if near is not None:
                arrays[key + "_" + label + "_near"] = near
            if series is not None:
                arrays[key + "_" + label + "_series"] = series
            raw[label] = {"RTA": actual.tolist(), "errors_vs_oracle": errors(actual, expected),
                          "max_abs_vs_oracle": difference, "physics": physics,
                          "invalid_rows": int(np.count_nonzero(invalid)),
                          "exact_critical_rows": int(np.count_nonzero(exact)),
                          "near_critical_rows": int(np.count_nonzero(near)) if near is not None else None,
                          "small_delta_layer_lanes": int(np.sum(series)) if series is not None else None,
                          "kernel_history": history,
                          "actual_backend_kernel_launch_verified": verified,
                          "gate_pass": passed}
        for input_name in ("wavelength_nm", "indices", "thickness_nm", "beta"):
            arrays[key + "_input_" + input_name] = getattr(batch, input_name)
        arrays[key + "_oracle_RTA"] = expected
        arrays[key + "_CPU_FP64_RTA"] = cpu
        report["cases"].append({"name": batch.name, "input_sha256": input_hash(batch),
                                "shape": [len(batch.wavelength_nm), 3],
                                "polarization": batch.polarization,
                                "wavelength_nm": batch.wavelength_nm.tolist(),
                                "beta": batch.beta.tolist(),
                                "indices": [[str(value) for value in row] for row in batch.indices],
                                "thickness_nm": batch.thickness_nm.tolist(),
                                "oracle_RTA": expected.tolist(), "CPU_FP64_RTA": cpu.tolist(),
                                "results": raw, "raw_npz_prefix": key})
    report["summary"] = {"physical_groups": len(batches) // len(POLARIZATIONS),
                         "polarized_cases": len(batches),
                         "original_failed_cases": original_failures,
                         "candidate_failed_cases": candidate_failures,
                         "candidate_all_pass": candidate_failures == 0,
                         "real_backend_verified": backend != "numpy" and all(
                             case["results"]["candidate"]["actual_backend_kernel_launch_verified"]
                             for case in report["cases"])}
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        npz = path.with_suffix(".npz")
        np.savez_compressed(npz, **arrays)
        report["raw_npz"] = npz.name
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                        encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("numpy", "cuda", "llvm"), default="numpy")
    parser.add_argument("--precision", choices=("float64", "float32"), default="float64")
    parser.add_argument("--case", action="append", help="name fragment; may be repeated")
    parser.add_argument("--oracle-fixture", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    proof = run(backend=args.backend, precision=args.precision, cases=args.case,
                oracle_fixture=args.oracle_fixture, output=args.output)
    print(json.dumps(proof["summary"], ensure_ascii=False, indent=2))
    return 0 if proof["summary"]["candidate_all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
