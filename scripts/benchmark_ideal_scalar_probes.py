#!/usr/bin/env python3
"""Bounded CPU-only scalar-F equivalence/performance microbenchmark.

No optimization, publication, IPC or historical-storage tests. Source n/k rows
are read only from a current checked benchmark fixture, not an API adapter.
Each subprocess has 60s wall/CPU and 1GiB address-space limits; the group stops
at 600 CPU seconds. Single-BLAS settings are applied before NumPy imports.
"""
from __future__ import annotations


import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
from typing import Literal

for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
              "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_name] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def source_hashes():
    """Bind actual measured implementations, not just their development base."""
    files = (
        "scripts/benchmark_ideal_scalar_probes.py",
        "coat_sim/core/film_ideal_sensitivity.py",
        "coat_sim/core/_film_ideal_sensitivity_core.py",
        "coat_sim/core/film_optimization_derivatives.py",
        "coat_sim/core/film_optics.py",
        "coat_sim/core/film_optimization_needle.py",
    )
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}


def fixture(layers, full_grid, source_nk):
    import numpy as np
    from coat_sim.core import film_ideal_sensitivity as ideal
    from coat_sim.core.film_design import validate_design
    from coat_sim.core.film_design_service import FilmDesignService
    from coat_sim.core.film_optimization import FilmObjectiveConstraint, FilmOptimizationObjective, FilmThicknessVariable

    source = ROOT / "benchmarks/film_synthesis/20261022/multispectral-v1/four-window-models/author_export-request.json"
    raw = source.read_bytes()
    captured = json.loads(raw)["request"]
    records = captured["frozen_material_snapshot"]["materials"]

    class Materials:
        def _records(self):
            return deepcopy(records)

        def get(self, identifier):
            return deepcopy(next(r for r in records if r["id"] == identifier))

    class Service:
        validate = staticmethod(validate_design)
        freeze_design = FilmDesignService.freeze_design
        materials = Materials()

    design = deepcopy(captured["environment"])
    design["use_k"] = source_nk
    design["assume_lossless"] = not source_nk
    film_ids = ("model_zns_film_amotchkina2020_original_export",
                "model_ybf3_film_amotchkina2020_original_export")
    design["front_layers"] = [dict(
        id=f"probe-film-{i}", material_id=film_ids[i % 2], optical_set_id="nk",
        thickness=dict(value=80.0 + i * 9.0, unit="nm"),
        note="microbenchmark only; not a qualified coating design",
    ) for i in range(layers)]
    angles = tuple(float(x) for x in np.arange(40, 50.1, .5)) if full_grid else (40., 45., 50.)
    bands: tuple[tuple[float, float, Literal["R", "T"], float], ...] = (
        (580., 920., "R", .97), (1039., 1089., "R", .99),
        (3650., 5000., "T", .98), (7650., 10550., "T", .98))
    objectives = tuple(FilmOptimizationObjective(lo, hi, int(hi - lo) + 1 if full_grid else 9, angles, "unpolarized", metric, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=bound, upper_bound=None, strict_lower=True), weight=(1., 2.3, .4, 3.7)[i], normalization_scale=(1., .8, .6, .9)[i]) for i, (lo, hi, metric, bound) in enumerate(bands))
    ids = tuple(f"new-band-{i}" for i in range(4))
    request = ideal.FilmIdealSensitivityRequest(
        design, 1, objectives,
        tuple(FilmThicknessVariable("front", row["id"], 0., 1000.)
              for row in design["front_layers"]), "relative",
        max_iterations=1, max_evaluations=None,
        policy="bounded_baseline_degradation", objective_ids=ids,
        budgets=tuple(ideal.FilmIdealDegradationBudget(i, .1) for i in ids),
    )
    frozen = ideal.freeze_film_ideal_sensitivity_request(Service(), request)
    return frozen, dict(source_sha256=hashlib.sha256(raw).hexdigest(),
                        material_model=("source original n/k, NOT qualified low-k or process evidence"
                                        if source_nk else "explicit zero-k assumption"))


def run_worker(case, measurement=None):
    import numpy as np
    from coat_sim.core import film_ideal_sensitivity as ideal
    from coat_sim.core import _film_ideal_sensitivity_core as core
    from coat_sim.core import film_optimization_derivatives as derivatives

    # These are enforceable CPU/address-space limits, not just post-hoc RSS claims.
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    frozen, evidence = fixture(case["layers"], case["full_grid"], case["source_nk"])
    design = frozen.design_snapshot
    physical = core._Evaluator(frozen).physical(design)
    center = core._Evaluator(frozen).scalar_objective(design)
    probes: list[dict] = []
    stencils: list[tuple[float, float, float, float]] = []
    for variable in frozen.variables:
        value = physical[variable.layer_id]
        x1, x2 = core._bounded_stencil(value, variable.lower_nm, variable.upper_nm)
        probes.extend(core._candidate_design(design, (variable,), (x,)) for x in (x1, x2))
        stencils.append((value, x1, x2, variable.upper_nm - variable.lower_nm))
    designs = [design] if case["operation"] == "F" else probes

    def values(evaluator, scalar):
        answer = []
        for changed in designs:
            # Both paths use identical disabled candidate-cache policy. Prepared
            # optical-series reuse remains enabled; scalar adds no result cache.
            evaluator.cache.clear()
            answer.append(evaluator.scalar_objective(changed) if scalar else
                          evaluator.evaluate(changed, probe=True).analysis.objective_value)
        if case["operation"] != "F":
            return [core._three_point_derivative(center, answer[2*i], answer[2*i+1],
                                                   value, x1, x2) * span
                    for i, (value, x1, x2, span) in enumerate(stencils)]
        return answer

    # Accuracy gate precedes every timing in this subprocess.
    reference = values(core._Evaluator(frozen), measurement is not None)
    if measurement is None:
        actual = values(core._Evaluator(frozen), True)
        if [x.hex() for x in actual] != [x.hex() for x in reference]:
            raise AssertionError("precision gate failed: scalar/reference are not bit-exact")
    calls = 0
    original = derivatives.coherent_rta

    def tracked(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    derivatives.coherent_rta = tracked
    rows = []
    for scalar in ((False, True) if measurement is None else (measurement["scalar"],)):
        for warm in ((False, True) if measurement is None else (measurement["warm"],)):
            evaluator = core._Evaluator(frozen) if warm else None
            if evaluator is not None:
                # Warm the same run-owned material series, never a candidate F.
                values(evaluator, scalar)
                evaluator.cache.clear()
                evaluator.evaluations = evaluator.forward = evaluator.jacobian = 0
                evaluator.probes = evaluator.cache_hits = 0
                evaluator.requested_coherent_value_solves = 0
            calls = 0
            wall, cpu = time.perf_counter(), time.process_time()
            if evaluator is None:
                evaluator = core._Evaluator(frozen)
            output = values(evaluator, scalar)
            elapsed, cpu_elapsed = time.perf_counter() - wall, time.process_time() - cpu
            assert [x.hex() for x in output] == [x.hex() for x in reference]
            rows.append(dict(path="scalar" if scalar else "complete", warm=warm,
                             wall_seconds=elapsed, cpu_seconds=cpu_elapsed,
                             logical_blocks=evaluator.forward,
                             jacobian_blocks=evaluator.jacobian,
                             coherent_value_solver_calls=calls,
                             output_float_hex=[x.hex() for x in output],
                             candidate_cache_hits=evaluator.cache_hits))
    resources = ideal.estimate_film_ideal_sensitivity_resources(frozen)
    return dict(case=case, evidence=evidence, precision="bit-exact same platform",
                condition_points=resources["condition_points"],
                condition_blocks=resources["condition_blocks_per_evaluation"],
                resource_estimate=resources, rows=rows,
                peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                total_cpu_seconds=resource.getrusage(resource.RUSAGE_SELF).ru_utime
                                  + resource.getrusage(resource.RUSAGE_SELF).ru_stime,
                numpy_version=np.__version__, longdouble_itemsize=np.dtype(np.longdouble).itemsize)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-json")
    parser.add_argument("--measurement-json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker_json:
        measurement = None if args.measurement_json is None else json.loads(args.measurement_json)
        print(json.dumps(run_worker(json.loads(args.worker_json), measurement), allow_nan=False))
        return
    if args.output is None:
        parser.error("--output is required")
    cases = [dict(layers=layers, full_grid=False, source_nk=False, operation=operation)
             for layers in (2, 8, 16) for operation in ("F", "2Vgradient")]
    cases += [dict(layers=2, full_grid=True, source_nk=source_nk, operation="F")
              for source_nk in (False, True)]
    results, total_cpu = [], 0.0
    for case in cases:
        proc = subprocess.run([sys.executable, __file__, "--worker-json", json.dumps(case)],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode:
            raise RuntimeError(f"{case}: {proc.stderr}")
        result = json.loads(proc.stdout)
        total_cpu += result["total_cpu_seconds"]
        expected_hex = result["rows"][0]["output_float_hex"]
        result["precision_gate_peak_rss_kib"] = result.pop("peak_rss_kib")
        isolated = []
        for scalar in (False, True):
            for warm in (False, True):
                measured = subprocess.run([
                    sys.executable, __file__, "--worker-json", json.dumps(case),
                    "--measurement-json", json.dumps(dict(scalar=scalar, warm=warm)),
                ], capture_output=True, text=True, timeout=60)
                if measured.returncode:
                    raise RuntimeError(f"{case}: {measured.stderr}")
                run = json.loads(measured.stdout)
                total_cpu += run["total_cpu_seconds"]
                row = run["rows"][0]
                if row["output_float_hex"] != expected_hex:
                    raise AssertionError("isolated measurement differs from accuracy gate")
                row["isolated_process_peak_rss_kib"] = run["peak_rss_kib"]
                isolated.append(row)
                if total_cpu > 600 or run["peak_rss_kib"] > 1024**2:
                    raise RuntimeError("group CPU or subprocess RSS limit exceeded")
        result["rows"] = isolated
        result["peak_rss_kib"] = max(r["isolated_process_peak_rss_kib"] for r in isolated)
        if total_cpu > 600 or result["precision_gate_peak_rss_kib"] > 1024**2:
            raise RuntimeError("group CPU or subprocess RSS limit exceeded")
        results.append(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(dict(
            schema_version=1, baseline_source_commit="c46bbbdc76f893eae0979555cfc6b1e1ad4b82c0",
            measured_source_sha256=source_hashes(),
            purpose="bounded scalar-F microbenchmark; no 89-layer optimization",
            cache_policy="both paths clear exact candidate cache before each probe; prepared optics reused",
            limits=dict(case_wall_seconds=60, case_cpu_seconds=60, group_cpu_seconds=600,
                        case_address_space_bytes=1024**3, case_rss_bytes=1024**3),
            blas_threads=1, results=results, total_cpu_seconds=total_cpu,
        ), indent=2, allow_nan=False) + "\n")
        print(json.dumps(dict(case=case, precision=result["precision"],
                              peak_rss_kib=result["peak_rss_kib"], total_cpu_seconds=total_cpu)),
              flush=True)


if __name__ == "__main__":
    main()
