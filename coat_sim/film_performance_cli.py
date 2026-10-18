"""JSON stdin/stdout entry for the opt-in frozen film batch service.

No live catalog access, files, queues, UI state, installation or credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from coat_sim.core.film_performance import FrozenFilmEvaluator, PerformanceOptions, KERNEL_VERSION, available_cpu_workers


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("capability", "evaluate"))
    parser.add_argument("--live-probe", action="store_true", help="Explicitly initialize and probe a GPU runtime")
    parser.add_argument("--backend", choices=("gpu", "cuda", "rocm", "xpu"), default="gpu")
    args = parser.parse_args()
    try:
        if args.operation == "capability":
            from coat_sim.core.film_optics_gpu import existing_torch_build_info
            try:
                build = existing_torch_build_info()
                torch, cuda = build.get("__version__", "unknown"), build.get("cuda")
            except (ImportError, OSError, ValueError) as exc:
                torch, cuda, build = str(exc), None, {}
            result = {"kernel_version": KERNEL_VERSION, "logical_cpus": os.cpu_count(),
                      "max_cpu_workers": available_cpu_workers(), "default_cpu_workers": 1,
                      "precision": "float64/complex128", "torch_build": torch,
                      "cuda_build": cuda, "hip_build": build.get("hip"), "xpu_build": build.get("xpu"),
                      "supported_gpu_runtimes": ["cuda", "rocm", "xpu"],
                      "gpu_live_verified": False,
                      "jacobian_backend": "CPU", "jacobian_domain": "single-front semi-infinite regular layers",
                      "live_catalog_access": False}
            if args.live_probe:
                from coat_sim.core.film_optics_gpu import gpu_capability
                probe = gpu_capability(args.backend)
                result.update(gpu_probe=probe, gpu_live_verified=probe["available"])
        else:
            if args.live_probe or args.backend != "gpu":
                raise ValueError("probe flags apply to capability only; evaluate uses performance.backend")
            raw = sys.stdin.buffer.read(16 * 1024**2 + 1)
            if len(raw) > 16 * 1024**2:
                raise ValueError("inline frozen input exceeds 16 MiB")
            request = json.loads(raw)
            allowed = {"frozen_snapshot", "settings", "layer_ids", "thickness_nm", "performance",
                       "zero_thickness_layer_ids"}
            if not isinstance(request, dict) or set(request) - allowed:
                raise ValueError("unknown batch request fields")
            snapshot = request.get("frozen_snapshot")
            if not isinstance(snapshot, dict):
                raise ValueError("frozen_snapshot must be an object")
            options = PerformanceOptions(**request.get("performance", {}))
            # JSON float lists carry more overhead than raw numeric arrays.
            # Explicit inline result cap; never subsample the spectrum.
            from coat_sim.core.film_design_service import simulation_settings
            settings = simulation_settings(request["settings"] if "settings" in request else snapshot.get("settings"))
            if len(request["thickness_nm"]) * settings["samples"] * 24 > 8 * 1024**2:
                raise ValueError("inline raw output exceeds 8 MiB; use fewer candidates or the Python service")
            evaluator = FrozenFilmEvaluator(request["frozen_snapshot"], request.get("settings"), options=options)
            result = evaluator.evaluate_thickness_batch(request["layer_ids"], request["thickness_nm"],
                zero_thickness_layer_ids=request.get("zero_thickness_layer_ids", ()))
            for key in ("wavelength_nm", "R", "T", "A"):
                result[key] = result[key].tolist()
        json.dump({"ok": True, "result": result}, sys.stdout, ensure_ascii=False, allow_nan=False)
        sys.stdout.write("\n")
        return 0
    except (KeyError, TypeError, ValueError, ArithmeticError, InterruptedError, ImportError) as exc:
        json.dump({"ok": False, "error": str(exc)}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
