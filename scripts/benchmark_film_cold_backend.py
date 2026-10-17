"""Run each mode in a fresh native process for fair cold end-to-end DE cost."""
# ruff: noqa: E402 -- source-path bootstrap and cold timer precede optional heavy imports.

import time

ENTRY = time.perf_counter()
from typing import Any
from importlib import import_module
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from coat_sim.core.film_optics_batch import FrozenCoherentStack
from coat_sim.core.film_performance import CoherentBatchEvaluator, PerformanceOptions
from coat_sim.core.film_batch_de import BatchEvaluation, run_batch_de
from scripts.benchmark_intel_film import peak_working_set


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["cpu", "gpu", "hybrid"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--layers", type=int, default=60)
    parser.add_argument("--waves", type=int, default=1001)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--cpu-fraction", type=float, default=0.5)
    args = parser.parse_args()
    if args.device_index < 0:
        parser.error("device-index must be nonnegative")
    waves = np.linspace(400, 800, args.waves)
    indices = np.empty((args.waves, args.layers + 2), np.complex128)
    indices[:, 0] = 1
    indices[:, -1] = 1.52
    for i in range(args.layers):
        indices[:, i + 1] = (2.1 + 0.002j if i % 2 else 1.45) + 0.01 * (550 / waves - 1)
    stack = FrozenCoherentStack.prepare(waves, indices, 37, "unpolarized")
    target = stack.evaluate(np.full((1, args.layers), 80.0))[0, :, 0]
    scratch = (32 if args.mode == "hybrid" else 64) * 1024**2
    cpu = CoherentBatchEvaluator(
        stack, PerformanceOptions(backend="cpu", cpu_workers=args.workers, chunk_bytes=scratch)
    )
    gpu = None
    gpu_init = 0.0
    trace = []
    cpu_times = []

    def getgpu():
        nonlocal gpu, gpu_init
        if gpu is None:
            begin = time.perf_counter()
            torch: Any = import_module("torch")

            torch.set_num_threads(1)
            torch.set_num_interop_threads(1)
            torch.xpu.set_device(args.device_index)
            if "intel" not in torch.xpu.get_device_name(args.device_index).lower():
                raise RuntimeError("Selected XPU must explicitly identify Intel")
            from coat_sim.core.film_optics_gpu import TorchCoherentBatch

            gpu = TorchCoherentBatch(stack, backend="xpu")
            gpu.verify(np.full((2, args.layers), 90.0))
            gpu_init = time.perf_counter() - begin
        return gpu

    with ThreadPoolExecutor(max_workers=1) as pool:

        def evaluate(points):
            trace.extend(points.copy())
            if args.mode == "cpu" or (args.mode == "hybrid" and len(points) == 1):
                values = cpu.evaluate(points)
            elif args.mode == "gpu":
                values = getgpu().evaluate(points)
            else:
                resident = getgpu()
                split = max(1, min(len(points) - 1, round(len(points) * args.cpu_fraction)))
                job = pool.submit(cpu.evaluate, points[:split])
                right = resident.evaluate(points[split:], chunk_bytes=32 * 1024**2)
                values = np.concatenate([job.result(), right])
            errors = values[:, :, 0] - target
            return [
                BatchEvaluation(
                    (float(np.max(np.abs(e))), float(np.sqrt(np.mean(e * e)))),
                    float(np.mean(e * e)),
                    None,
                )
                for e in errors
            ]

        def run():
            trace.clear()
            begin = time.perf_counter()
            cpu_begin = time.process_time()
            outcome = run_batch_de(
                evaluate,
                np.full(args.layers, 90.0).tolist(),
                np.full(args.layers, 30.0).tolist(),
                np.full(args.layers, 150.0).tolist(),
                max_iterations=3,
                max_evaluations=256,
                population_size=64,
                max_batch_size=64,
                seed=19,
            )
            cpu_times.append(time.process_time() - cpu_begin)
            return time.perf_counter() - begin, outcome

        preopt = time.perf_counter() - ENTRY
        cold, outcome = run()
        cold_total = time.perf_counter() - ENTRY
        warm = []
        for _ in range(3):
            seconds, outcome = run()
            warm.append(seconds)
    result = {
        "mode": args.mode,
        "layers": args.layers,
        "waves": args.waves,
        "cpu_workers": args.workers,
        "cpu_fraction": args.cpu_fraction,
        "seed": 19,
        "preoptimization_import_and_prepare_seconds": preopt,
        "cold_optimizer_seconds": cold,
        "cold_process_work_seconds": cold_total,
        "gpu_import_init_and_gate_seconds": gpu_init,
        "warm_optimizer_seconds": warm,
        "warm_median_seconds": statistics.median(warm),
        "process_cpu_seconds_cold_then_warm": cpu_times,
        "warm_mean_occupied_cpu_cores": sum(cpu_times[1:]) / sum(warm),
        "peak_process_working_set_bytes": peak_working_set(),
        "evaluations": outcome.evaluations,
        "failed": outcome.failed,
        "state": outcome.state,
        "reason": outcome.reason,
        "best_key": outcome.best.key,
        "best_thickness": outcome.best_thickness.tolist(),
        "trace_sha256": hashlib.sha256(np.array(trace).tobytes()).hexdigest(),
        "gpu_device": gpu.device_name if gpu else None,
        "last_gpu_timing": gpu.last_timing if gpu else None,
        "cold_definition": "new Python process; includes SciPy first import equally, and GPU runtime import/init/gate in GPU modes; existing driver disk cache retained",
    }
    args.output.write_text(json.dumps(result, indent=2), encoding="utf8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
