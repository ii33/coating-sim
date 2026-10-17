"""Native FP64 CPU/XPU/independent-candidate cooperation benchmark.
# ruff: noqa: E402 -- source-path bootstrap and cold timer precede optional heavy imports.

No calibration is installed and no product setting is changed. Set BLAS/OMP
threads to 1 in the parent process; all CPU workers split independent candidates.
Run in a bounded supervisor for diagnostic safety, never count a timeout as pass.
"""

from __future__ import annotations
from typing import Any
from importlib import import_module
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from coat_sim.core.film_optics_batch import FrozenCoherentStack
from coat_sim.core.film_optics_gpu import TorchCoherentBatch
from coat_sim.core.film_performance import (
    CoherentBatchEvaluator,
    PerformanceOptions,
    available_cpu_workers,
    KERNEL_VERSION,
)
from coat_sim.core.film_batch_de import BatchEvaluation, run_batch_de


def peak_working_set():
    if sys.platform != "win32":
        return None

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (key, ctypes.c_size_t)
            for key in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
            )
        ]

    value = Counters()
    value.cb = ctypes.sizeof(value)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Counters),
        wintypes.DWORD,
    ]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return value.PeakWorkingSetSize


def measure(fn, repetitions):
    times = []
    cpu = []
    result = None
    for _ in range(repetitions):
        begin = time.perf_counter()
        cpubegin = time.process_time()
        result = fn()
        times.append(time.perf_counter() - begin)
        cpu.append(time.process_time() - cpubegin)
    return result, {
        "wall_seconds": times,
        "median_seconds": statistics.median(times),
        "process_cpu_seconds": cpu,
        "mean_occupied_cpu_cores": sum(cpu) / sum(times),
        "peak_process_working_set_bytes": peak_working_set(),
    }


def model(layers, waves):
    # Passive weakly absorbing dispersive stack at oblique incidence. Same
    # exact arrays/seed in all modes; no scalar-service baseline handicap.
    wavelengths = np.linspace(400, 800, waves)
    indices = np.empty((waves, layers + 2), np.complex128)
    indices[:, 0] = 1.0
    indices[:, -1] = 1.52
    for i in range(layers):
        indices[:, i + 1] = (2.1 + 0.002j if i % 2 else 1.45) + 0.01 * (550 / wavelengths - 1)
    return FrozenCoherentStack.prepare(wavelengths, indices, 37, "unpolarized")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--layers", nargs="+", type=int, default=[2, 60, 120])
    parser.add_argument("--waves", nargs="+", type=int, default=[81, 1001])
    parser.add_argument("--batches", nargs="+", type=int, default=[16, 128])
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--workers", nargs="+", type=int)
    parser.add_argument(
        "--hybrid-workers",
        nargs="+",
        type=int,
        help="Optional independent CPU worker scan for hybrid subsets",
    )
    parser.add_argument("--optimization", action="store_true")
    parser.add_argument("--device-index", type=int, default=0)
    args = parser.parse_args()
    if args.device_index < 0:
        parser.error("device-index must be nonnegative")
    if (
        args.repetitions < 3
        or any(x < 1 for x in args.layers + args.waves)
        or any(x < 2 for x in args.batches)
    ):
        parser.error("positive layers/wavelengths, batches >=2 and at least 3 repetitions required")
    import_begin = time.perf_counter()
    torch: Any = import_module("torch")

    torch_import_seconds = time.perf_counter() - import_begin
    context_begin = time.perf_counter()
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.xpu.set_device(args.device_index)
    properties = torch.xpu.get_device_properties(args.device_index)
    context_seconds = time.perf_counter() - context_begin
    if "intel" not in properties.name.lower():
        raise RuntimeError("Selected XPU must explicitly identify Intel")
    if not properties.has_fp64:
        raise RuntimeError("Selected Intel XPU does not expose FP64; keep CPU")
    limit = available_cpu_workers()
    workers = sorted(set(args.workers or [x for x in [1, 2, 4, 8, 12, 16, limit] if x <= limit]))
    if any(x < 1 or x > limit for x in workers):
        parser.error("workers exceed process CPU availability")
    report = {
        "kernel_version": KERNEL_VERSION,
        "host": platform.node(),
        "platform": platform.platform(),
        "python": sys.version,
        "numpy": np.__version__,
        "torch": torch.__version__,
        "torch_import_seconds": torch_import_seconds,
        "xpu_context_seconds": context_seconds,
        "pid": os.getpid(),
        "available_process_cpus": limit,
        "workers_scan": workers,
        "intel_device_index": args.device_index,
        "intel_properties": str(properties),
        "thread_environment": {
            k: os.environ.get(k)
            for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")
        },
        "seed": 19,
        "rtol": 2e-12,
        "atol": 2e-12,
        "cases": [],
        "compile_policy": "eager; no explicit graph compilation; driver/kernel first-use cost included in cold call",
        "heterogeneous_policy": "benchmark only; independent contiguous candidate split, ordered concatenation; CPU and XPU scratch budgets total 64 MiB",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, indent=2), encoding="utf8")

    save()
    for layers in args.layers:
        for waves in args.waves:
            begin = time.perf_counter()
            stack = model(layers, waves)
            prepare = time.perf_counter() - begin
            begin = time.perf_counter()
            gpu = TorchCoherentBatch(stack, backend="xpu")
            init = time.perf_counter() - begin
            for count in args.batches:
                print(f"CASE layers={layers} waves={waves} count={count}", flush=True)
                d = np.random.default_rng(19).uniform(30, 150, (count, layers))
                reference = stack.evaluate(d)
                row = {
                    "layers": layers,
                    "waves": waves,
                    "candidates": count,
                    "prepared_seconds": prepare,
                    "gpu_init_seconds": init,
                    "resident_upload_seconds": gpu.upload_seconds,
                    "cpu": {},
                    "hybrid": {},
                }
                for n in workers:
                    cpu = CoherentBatchEvaluator(
                        stack, PerformanceOptions(backend="cpu", cpu_workers=n)
                    )
                    _, cold = measure(lambda cpu=cpu, d=d: cpu.evaluate(d), 1)
                    actual, timing = measure(lambda cpu=cpu, d=d: cpu.evaluate(d), args.repetitions)
                    np.testing.assert_allclose(actual, reference, rtol=2e-12, atol=2e-12)
                    row["cpu"][str(n)] = dict(timing, cold_seconds=cold["median_seconds"])
                best = int(min(row["cpu"], key=lambda n: row["cpu"][n]["median_seconds"]))
                row["best_cpu_workers"] = best
                actual, cold = measure(lambda gpu=gpu, d=d: gpu.evaluate(d), 1)
                np.testing.assert_allclose(actual, reference, rtol=2e-12, atol=2e-12)
                gatebegin = time.perf_counter()
                gate = gpu.verify(d)
                gate_seconds = time.perf_counter() - gatebegin
                actual, timing = measure(lambda gpu=gpu, d=d: gpu.evaluate(d), args.repetitions)
                np.testing.assert_allclose(actual, reference, rtol=2e-12, atol=2e-12)
                row["gpu"] = dict(
                    timing,
                    cold_seconds=cold["median_seconds"],
                    verification_seconds=gate_seconds,
                    verification_max_abs_error=gate,
                    max_abs_error=float(np.max(np.abs(actual - reference))),
                    **gpu.last_timing,
                )
                for fraction in [0.25, 0.5, 0.75]:
                    for hybrid_workers in args.hybrid_workers or [best]:
                        split = max(1, min(count - 1, round(count * fraction)))
                        cpu = CoherentBatchEvaluator(
                            stack,
                            PerformanceOptions(
                                backend="cpu", cpu_workers=hybrid_workers, chunk_bytes=32 * 1024**2
                            ),
                        )
                        with ThreadPoolExecutor(max_workers=1) as pool:

                            def hybrid(cpu=cpu, d=d, split=split, gpu=gpu, pool=pool):
                                job = pool.submit(cpu.evaluate, d[:split])
                                right = gpu.evaluate(d[split:], chunk_bytes=32 * 1024**2)
                                return np.concatenate([job.result(), right], axis=0)

                            hybrid()
                            actual, timing = measure(hybrid, args.repetitions)
                        np.testing.assert_allclose(actual, reference, rtol=2e-12, atol=2e-12)
                        row["hybrid"][f"{fraction}/{hybrid_workers}"] = dict(
                            timing,
                            cpu_candidates=split,
                            gpu_candidates=count - split,
                            cpu_workers=hybrid_workers,
                            max_abs_error=float(np.max(np.abs(actual - reference))),
                            gpu_last_timing=dict(gpu.last_timing),
                        )
                cpu_time = row["cpu"][str(best)]["median_seconds"]
                row["cpu_over_gpu"] = cpu_time / row["gpu"]["median_seconds"]
                row["cpu_over_best_hybrid"] = cpu_time / min(
                    x["median_seconds"] for x in row["hybrid"].values()
                )
                report["cases"].append(row)
                save()
                print(
                    json.dumps(
                        {
                            "shape": [layers, waves, count],
                            "best_workers": best,
                            "cpu_seconds": cpu_time,
                            "gpu_seconds": row["gpu"]["median_seconds"],
                            "cpu_over_gpu": row["cpu_over_gpu"],
                            "cpu_over_best_hybrid": row["cpu_over_best_hybrid"],
                        }
                    ),
                    flush=True,
                )
            if args.optimization:
                # Same deferred DE, budget, objectives, proposal order and seed in
                # all modes. No parallelization of dependent generations.
                count = min(64, max(args.batches))
                base = np.full(layers, 80.0)
                target = stack.evaluate(base[None, :])[0, :, 0]
                best = int(report["cases"][-1]["best_cpu_workers"])
                cpu = CoherentBatchEvaluator(
                    stack,
                    PerformanceOptions(backend="cpu", cpu_workers=best, chunk_bytes=32 * 1024**2),
                )
                optimization = {}
                for mode in ["cpu", "gpu", "hybrid"]:
                    traces: list[Any] = []
                    with ThreadPoolExecutor(max_workers=1) as pool:

                        def evaluate(
                            points,
                            traces=traces,
                            mode=mode,
                            cpu=cpu,
                            gpu=gpu,
                            target=target,
                            pool=pool,
                        ):
                            traces.extend(points.copy())
                            if mode == "cpu":
                                values = cpu.evaluate(points)
                            elif mode == "gpu":
                                values = gpu.evaluate(points)
                            elif len(points) == 1:
                                values = cpu.evaluate(points)
                            else:
                                split = max(1, len(points) // 2)
                                job = pool.submit(cpu.evaluate, points[:split])
                                right = gpu.evaluate(points[split:], chunk_bytes=32 * 1024**2)
                                values = np.concatenate([job.result(), right])
                            error = values[:, :, 0] - target
                            return [
                                BatchEvaluation(
                                    (float(np.max(np.abs(e))), float(np.sqrt(np.mean(e * e)))),
                                    float(np.mean(e * e)),
                                    None,
                                )
                                for e in error
                            ]

                        def optimize(traces=traces, layers=layers, count=count, evaluate=evaluate):
                            traces.clear()
                            return run_batch_de(
                                evaluate,
                                np.full(layers, 90.0).tolist(),
                                np.full(layers, 30.0).tolist(),
                                np.full(layers, 150.0).tolist(),
                                max_iterations=3,
                                max_evaluations=count * 4,
                                population_size=count,
                                max_batch_size=count,
                                seed=19,
                            )

                        outcome, cold = measure(optimize, 1)
                        outcome, timing = measure(optimize, args.repetitions)
                        optimization[mode] = dict(
                            timing,
                            cold_seconds=cold["median_seconds"],
                            best_key=outcome.best.key,
                            best_thickness=outcome.best_thickness.tolist(),
                            evaluations=outcome.evaluations,
                            failed=outcome.failed,
                            state=outcome.state,
                            reason=outcome.reason,
                            trace_sha256=hashlib.sha256(np.array(traces).tobytes()).hexdigest(),
                        )
                expected = optimization["cpu"]
                for other in ["gpu", "hybrid"]:
                    actual = optimization[other]
                    np.testing.assert_allclose(
                        actual["best_key"], expected["best_key"], rtol=2e-12, atol=2e-12
                    )
                    np.testing.assert_array_equal(
                        actual["best_thickness"], expected["best_thickness"]
                    )
                    assert (
                        actual["evaluations"],
                        actual["state"],
                        actual["reason"],
                        actual["failed"],
                    ) == (expected["evaluations"], expected["state"], expected["reason"], 0)
                report.setdefault("optimization", []).append(
                    {
                        "layers": layers,
                        "waves": waves,
                        "population": count,
                        "max_iterations": 3,
                        "max_evaluations": count * 4,
                        "modes": optimization,
                        "same_proposal_trace": len(
                            {v["trace_sha256"] for v in optimization.values()}
                        )
                        == 1,
                        "same_best_thickness": True,
                        "best_key_allclose": True,
                    }
                )
                save()
    report["complete"] = True
    save()


if __name__ == "__main__":
    main()
