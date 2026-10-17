"""Fair single/64-candidate CPU axis/thread scan versus explicit Intel XPU.

Both backends use CoherentBatchEvaluator.evaluate and identical warm wall timing.
No product calibration is installed. Run phases sequentially on native hardware.
"""

import time

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
from typing import Any

ENTRY = time.perf_counter()  # Before numerical imports/context/first call.


def write(path, value):
    path.write_text(json.dumps(value, indent=2), encoding="utf8")


def evaluator(stack, backend, workers=1, axis="auto"):
    from coat_sim.core.film_performance import CoherentBatchEvaluator, PerformanceOptions

    return CoherentBatchEvaluator(
        stack, PerformanceOptions(backend=backend, cpu_workers=workers, cpu_parallel_axis=axis)
    )


def call(evaluator, points):
    import numpy as np

    start = time.perf_counter()
    cstart = time.process_time()
    result = evaluator.evaluate(points)
    timing: dict[str, Any] = {
        "seconds": time.perf_counter() - start,
        "process_cpu_seconds": time.process_time() - cstart,
        "metadata": dict(evaluator.last_metadata),
    }
    if evaluator.options.backend == "xpu":
        assert timing["metadata"]["backend"] == "gpu" and timing["metadata"]["gpu_runtime"] == "xpu"
        assert (
            "intel" in timing["metadata"]["device"].lower()
            and timing["metadata"]["output_device"] == "xpu:0"
        )
    else:
        assert timing["metadata"]["backend"] == "cpu"
    assert result.dtype == np.float64 and np.isfinite(result).all()
    return result, timing


def stats(rows):
    from scripts.benchmark_intel_film import peak_working_set

    return {
        "runs": rows,
        "median_seconds": statistics.median(r["seconds"] for r in rows),
        "mean_occupied_cpu_cores": sum(r["process_cpu_seconds"] for r in rows)
        / sum(r["seconds"] for r in rows),
        "peak_process_working_set_bytes": peak_working_set(),
    }


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import numpy as np
    from coat_sim.core.film_performance import available_cpu_workers, KERNEL_VERSION
    from scripts.benchmark_intel_film import model, peak_working_set

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=["scan", "confirm", "cold"], required=True)
    parser.add_argument("--backend", choices=["cpu", "xpu"], default="cpu")
    parser.add_argument("--candidates", choices=[1, 64], type=int, default=1)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prepare = time.perf_counter()
    stack = model(60, 10000)
    points = np.random.default_rng(19).uniform(30, 150, (64, 60))
    prepare = time.perf_counter() - prepare
    base: dict[str, Any] = {
        "shape": {
            "layers": 60,
            "waves": 10000,
            "angles": [37.0],
            "targets_per_candidate": 10000,
            "R_residuals_per_candidate": 10000,
        },
        "kernel_version": KERNEL_VERSION,
        "pid": os.getpid(),
        "numpy": np.__version__,
        "prepare_seconds": prepare,
        "candidate_sha256": hashlib.sha256(points.tobytes()).hexdigest(),
        "thread_environment": {
            k: os.environ.get(k)
            for k in ["OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"]
        },
        "timing": "same CoherentBatchEvaluator.evaluate wrapper; wall includes validation, thread creation/merge or candidate upload/device sync/download; frozen stack and resident arrays prepared separately",
    }
    if args.phase == "scan":
        record: dict[str, Any] = dict(base, cases={}, complete=False)
        workers = sorted(
            {min(available_cpu_workers(), v) for v in [1, 2, 4, 8, 12, 16, available_cpu_workers()]}
        )
        for count in [1, 64]:
            d = points[:count]
            rows: list[dict[str, Any]] = []
            reference = None
            for axis in ["candidates", "wavelengths"]:
                for n in workers:
                    if axis == "candidates" and count == 1 and n != 1:
                        continue
                    if axis == "wavelengths" and n == 1:
                        # Identical serial route: reuse measurement, avoid duplication.
                        rows.append(
                            dict(
                                rows[0],
                                requested_axis="wavelengths",
                                shared_serial_measurement=True,
                            )
                        )
                        continue
                    engine = evaluator(stack, "cpu", n, axis)
                    actual, first = call(engine, d)
                    scan_warm = []
                    for _ in range(3):
                        actual, timing = call(engine, d)
                        scan_warm.append(timing)
                    if reference is None:
                        reference = actual.copy()
                    np.testing.assert_allclose(actual, reference, rtol=2e-12, atol=2e-12)
                    row = dict(
                        stats(scan_warm),
                        requested_axis=axis,
                        requested_workers=n,
                        first_call=first,
                        max_abs_error=float(np.max(np.abs(actual - reference))),
                    )
                    rows.append(row)
                    record["cases"][str(count)] = rows
                    write(args.output_dir / "scan.json", record)
                    print(
                        json.dumps(
                            {
                                "candidates": count,
                                "axis": axis,
                                "workers": n,
                                "seconds": row["median_seconds"],
                            }
                        ),
                        flush=True,
                    )
        record["selected"] = {
            count: {
                "axis": (best := min(rows, key=lambda r: r["median_seconds"]))["requested_axis"],
                "workers": best["requested_workers"],
                "scan_seconds": best["median_seconds"],
            }
            for count, rows in record["cases"].items()
        }
        record["complete"] = True
        write(args.output_dir / "scan.json", record)
        print("SELECTED " + json.dumps(record["selected"]), flush=True)
        return
    selected = json.loads((args.output_dir / "scan.json").read_text(encoding="utf8"))["selected"]
    if args.phase == "cold":
        choice = selected[str(args.candidates)]
        engine = evaluator(stack, args.backend, choice["workers"], choice["axis"])
        actual, first = call(engine, points[: args.candidates])
        record = dict(
            base,
            backend=args.backend,
            candidates=args.candidates,
            choice=choice,
            first_common_call=first,
            cold_process_work_seconds=time.perf_counter() - ENTRY,
            peak_process_working_set_bytes=peak_working_set(),
            complete=True,
            cold_definition="fresh native Python process and first evaluator call; XPU import/context, resident upload and numerical gate included; existing driver disk cache retained",
        )
        write(args.output_dir / f"cold-{args.backend}-{args.candidates}.json", record)
        print(json.dumps(record), flush=True)
        return
    record = dict(base, cases={}, complete=False)
    for count in [1, 64]:
        choice = selected[str(count)]
        d = points[:count]
        engines = {
            "cpu": evaluator(stack, "cpu", choice["workers"], choice["axis"]),
            "xpu": evaluator(stack, "xpu", choice["workers"], choice["axis"]),
        }
        first = {}
        warm: dict[str, list[dict[str, Any]]] = {"cpu": [], "xpu": []}
        values = {}
        errors = []
        for backend, engine in engines.items():
            values[backend], first[backend] = call(engine, d)
        for rep in range(7):
            order = ["cpu", "xpu"] if rep % 2 == 0 else ["xpu", "cpu"]
            for backend in order:
                values[backend], timing = call(engines[backend], d)
                warm[backend].append(timing)
            np.testing.assert_allclose(values["cpu"], values["xpu"], rtol=2e-12, atol=2e-12)
            errors.append(float(np.max(np.abs(values["cpu"] - values["xpu"]))))
        record["cases"][str(count)] = {
            "cpu_choice": choice,
            "first_calls": first,
            "cpu": stats(warm["cpu"]),
            "xpu": stats(warm["xpu"]),
            "cpu_over_xpu": statistics.median(r["seconds"] for r in warm["cpu"])
            / statistics.median(r["seconds"] for r in warm["xpu"]),
            "max_abs_error": max(errors),
            "order": "alternating CPU/XPU and XPU/CPU across 7 repeats",
        }
        write(args.output_dir / "confirmed.json", record)
        print(
            "CONFIRMED "
            + json.dumps(
                {
                    "candidates": count,
                    "cpu": record["cases"][str(count)]["cpu"]["median_seconds"],
                    "xpu": record["cases"][str(count)]["xpu"]["median_seconds"],
                }
            ),
            flush=True,
        )
    record["complete"] = True
    write(args.output_dir / "confirmed.json", record)


if __name__ == "__main__":
    main()
