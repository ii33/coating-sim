"""Explicit optional native XPU precision/audit acceptance; no automatic install."""
# ruff: noqa: E402 -- source-path bootstrap and cold timer precede optional heavy imports.

from typing import Any, cast
from importlib import import_module
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np

torch: Any = import_module("torch")
TorchDispatchMode: Any = import_module("torch.utils._python_dispatch").TorchDispatchMode
tree_leaves = import_module("torch.utils._pytree").tree_leaves
from coat_sim.core.film_optics_batch import FrozenCoherentStack
from coat_sim.core.film_optics_gpu import TorchCoherentBatch
from coat_sim.core.film_performance import CoherentBatchEvaluator, PerformanceOptions


class DeviceAudit(TorchDispatchMode):
    def __init__(self, device):
        self.device = device
        self.ops: Counter[str] = Counter()

    def __torch_dispatch__(self, fn, types, args=(), kwargs=None):
        value = fn(*args, **(kwargs or {}))
        self.ops[str(fn)] += 1
        for tensor in tree_leaves(value):
            if isinstance(tensor, torch.Tensor) and tensor.device != self.device:
                raise AssertionError(f"CPU/other-device tensor in forward: {fn}: {tensor.device}")
        if "pow." in str(fn):
            raise AssertionError("scalar pow regression")
        return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device-index", type=int, default=0)
    args = parser.parse_args()
    if args.device_index < 0:
        parser.error("device-index must be nonnegative")
    torch.xpu.set_device(args.device_index)
    properties = torch.xpu.get_device_properties(args.device_index)
    if "intel" not in properties.name.lower() or not properties.has_fp64:
        raise RuntimeError("Selected device must identify Intel with FP64 support")
    rows = []
    definitions = [
        ("bare", 0, [1, 1.52], 0),
        ("absorbing", 2, [1, 2 + 0.1j, 1.4, 1.52 + 0.03j], 43),
        ("evanescent", 2, [2, 1, 1.4, 1.52], 47),
        ("high_reflector", 60, [1] + [2.1 if i % 2 else 1.45 for i in range(60)] + [1.52], 37),
        (
            "many_absorbing",
            120,
            [1] + [2.1 + 0.003j if i % 2 else 1.45 for i in range(120)] + [1.52],
            37,
        ),
        ("thick", 2, [1, 1.5, 2.1, 1.52], 37),
    ]
    for name, layers, indices, angle in definitions:
        for pol in ["s", "p", "unpolarized"]:
            stack = FrozenCoherentStack.prepare(np.linspace(400, 800, 81), indices, angle, pol)
            d = np.random.default_rng(19).uniform(0, 150, (5, layers))
            if name == "thick":
                d[:] = np.random.default_rng(19).uniform(1e5, 1e6, d.shape)
            if layers:
                d[-1] = 0
            gpu = TorchCoherentBatch(stack, backend="xpu")
            audit = DeviceAudit(gpu.device)
            original = gpu._compute

            def audited(*a, original=original, audit=audit, **kw):
                with audit:
                    return original(*a, **kw)

            cast(Any, gpu)._compute = audited
            begin = time.perf_counter()
            actual = gpu.evaluate(d)
            seconds = time.perf_counter() - begin
            reference = stack.evaluate(d)
            np.testing.assert_allclose(actual, reference, rtol=2e-12, atol=2e-12)
            # Deliberately small scratch budget is a constrained test, not a
            # claim of acceptance on an old/small-memory physical Intel GPU.
            tile_bytes = 1024 if layers <= 2 else 64 * 1024
            tiled = gpu.evaluate(d, chunk_bytes=tile_bytes)
            np.testing.assert_allclose(tiled, reference, rtol=2e-12, atol=2e-12)
            cancel = threading.Event()
            cancel.set()
            try:
                gpu.evaluate(d, cancel=cancel)
            except InterruptedError:
                pass
            else:
                raise AssertionError("cancellation lost")
            rows.append(
                {
                    "case": name,
                    "polarization": pol,
                    "layers": layers,
                    "max_abs_error": float(np.max(np.abs(actual - reference))),
                    "seconds": seconds,
                    "small_scratch_bytes": tile_bytes,
                    "device": str(gpu.device),
                    "dtype": str(actual.dtype),
                    "ops": dict(audit.ops),
                }
            )
            print(json.dumps(rows[-1]), flush=True)
    critical = FrozenCoherentStack.prepare([500.0, 600.0], [2.0, 1.0, 1.5], beta=1.0)
    evaluator = CoherentBatchEvaluator(critical, PerformanceOptions(backend="xpu"))
    actual = evaluator.evaluate([[80.0]])
    np.testing.assert_array_equal(actual, critical.evaluate([[80.0]]))
    assert (
        evaluator.last_metadata["backend"] == "cpu"
        and "critical" in evaluator.last_metadata["fallback_reason"]
    )
    result = {
        "complete": True,
        "properties": str(properties),
        "torch": torch.__version__,
        "cases": rows,
        "critical_fallback": evaluator.last_metadata,
        "audit_scope": "All ATen forward tensor outputs stay on selected XPU; no CPU tensor operators or pow. Driver internals not traced.",
        "limited_memory_test": "1024/65536-byte scratch tiling simulation only; not physical old hardware acceptance",
    }
    args.output.write_text(json.dumps(result, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
