"""Resident FP64 spectra using optional existing CUDA, ROCm or XPU PyTorch.

No DirectML/FP32 substitution and no driver or runtime installation. Exact or
near-critical layers are delegated to the CPU reference by the policy module.
Imports torch lazily: CPU callers never initialize a GPU context.
"""
from __future__ import annotations

import math
import time
import ast
from importlib.util import find_spec
from pathlib import Path
import numpy as np


def existing_torch_build_info():
    """Read the installed vendor version declaration without loading torch DLLs."""
    spec = find_spec("torch")
    if spec is None or spec.origin is None:
        raise ImportError("PyTorch is not installed")
    declarations = ast.parse(Path(spec.origin).with_name("version.py").read_text(encoding="utf-8"))
    fields = {}
    for node in declarations.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in {"__version__", "cuda", "hip", "xpu"}:
                fields[target.id] = ast.literal_eval(node.value)
    return fields


def existing_torch_build():
    """Legacy two-field CUDA build query, retained for callers."""
    fields = existing_torch_build_info()
    return fields.get("__version__", "unknown"), fields.get("cuda")


def select_gpu_runtime(torch, requested="gpu"):
    """Dispatch by runtime capability, never GPU marketing name or CUDA alone."""
    if requested not in {"gpu", "cuda", "rocm", "xpu"}:
        raise ValueError("GPU runtime must be gpu, cuda, rocm or xpu")
    hip = getattr(torch.version, "hip", None)
    candidates = (["rocm" if hip else "cuda", "xpu"]
                  if requested == "gpu" else [requested])
    reasons = []
    for backend in candidates:
        if backend == "rocm" and not hip:
            reasons.append("ROCm PyTorch build is not installed")
            continue
        if backend == "cuda" and (hip or not getattr(torch.version, "cuda", None)):
            reasons.append("NVIDIA CUDA PyTorch build is not installed")
            continue
        namespace = "cuda" if backend in {"cuda", "rocm"} else "xpu"
        runtime = getattr(torch, namespace, None)
        if runtime is None or not runtime.is_available():
            reasons.append(f"{backend} has no available device")
            continue
        device = torch.device(namespace, runtime.current_device())
        return backend, runtime, device
    raise RuntimeError("; ".join(reasons))


def verify_gpu_dtype(torch, runtime, device):
    """Actual FP64/complex128 operators; no implicit FP32 substitution."""
    if getattr(device, "type", None) == "xpu":
        properties = runtime.get_device_properties(device)
        if not getattr(properties, "has_fp64", False):
            raise RuntimeError("XPU device does not report FP64 support; CPU required")
    sample = np.array([1 + .2j, 2 + .3j], dtype=np.complex128)
    tensor = torch.tensor(sample, dtype=torch.complex128, device=device)
    if tensor.dtype != torch.complex128:
        raise RuntimeError("GPU failed to retain complex128 precision")
    actual = torch.exp(1j * tensor).cpu().numpy()
    runtime.synchronize(device)
    if actual.dtype != np.complex128 or not np.allclose(
            actual, np.exp(1j * sample), rtol=1e-13, atol=1e-13):
        raise RuntimeError("GPU complex128 capability check failed")



def gpu_capability(backend="gpu"):
    """Live probe; callers should use a bounded probe process, not a UI thread."""
    try:
        import torch
        selected, runtime, device = select_gpu_runtime(torch, backend)
        verify_gpu_dtype(torch, runtime, device)
        properties = runtime.get_device_properties(device)
        return {"available": True, "backend": selected,
                "device": runtime.get_device_name(device),
                "total_memory_bytes": int(properties.total_memory),
                "torch_version": torch.__version__,
                "cuda_version": getattr(torch.version, "cuda", None),
                "hip_version": getattr(torch.version, "hip", None),
                "dtype": "float64/complex128"}
    except (ImportError, RuntimeError, OSError, ValueError, AttributeError, NotImplementedError) as exc:
        return {"available": False, "requested_backend": backend, "reason": str(exc)}


def cuda_capability():
    """Legacy explicit NVIDIA probe; generic callers use gpu_capability."""
    return gpu_capability("cuda")


def gpu_domain_reason(stack):
    """Conservative project support domain; never change the optical model."""
    if np.any(stack.critical):
        return "exact critical internal layer uses CPU analytic limit"
    if np.any(np.abs(stack.q) < 1e-10 * np.maximum(1, np.abs(stack.indices))):
        return "near critical medium uses CPU reference"
    return None


def _squared_magnitude(torch, value):
    """Keep complex abs semantics without the XPU FP64 scalar-pow kernel.

    On tested Intel Windows runtimes ``abs(value) ** 2`` can stall in
    aten.pow.Tensor_Scalar. Multiplying the same FP64 magnitude is equivalent
    and avoids that operator; no precision or optical-model change is made.
    """
    magnitude = torch.abs(value)
    return magnitude * magnitude


class TorchCoherentBatch:
    """One stack's invariant arrays stay resident across bounded evaluations."""

    def __init__(self, stack, *, backend="gpu"):
        reason = gpu_domain_reason(stack)
        if reason:
            raise ValueError(reason)
        import torch
        self.backend, self.runtime, self.device = select_gpu_runtime(torch, backend)
        verify_gpu_dtype(torch, self.runtime, self.device)
        self.torch = torch
        self.stack = stack
        self.device_name = self.runtime.get_device_name(self.device)
        begin = time.perf_counter()
        self.waves = torch.tensor(stack.waves, dtype=torch.float64, device=self.device)
        self.q = torch.tensor(stack.q, dtype=torch.complex128, device=self.device)
        self.interfaces = {
            p: tuple(torch.tensor(a, dtype=torch.complex128, device=self.device) for a in pair)
            for p, pair in stack.interfaces.items()}
        self.fluxes = {
            p: tuple(torch.tensor(a, dtype=torch.float64, device=self.device) for a in pair)
            for p, pair in stack.fluxes.items()}
        self.runtime.synchronize(self.device)
        self.upload_seconds = time.perf_counter() - begin
        self.verified = False
        self.last_timing = {}

    def _compute(self, thickness, wave_start, wave_stop, cancel=None):
        torch = self.torch
        selection = slice(wave_start, wave_stop)
        waves = self.waves[selection]
        q = self.q[selection]
        channels = []
        for p, (ri_all, ti_all) in self.interfaces.items():
            ri, ti = ri_all[selection], ti_all[selection]
            r = ri[:, -1].unsqueeze(0).expand(thickness.shape[0], -1)
            t = ti[:, -1].unsqueeze(0).expand(thickness.shape[0], -1)
            for index in range(q.shape[1] - 3, -1, -1):
                if index % 16 == 0 and cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()):
                    raise InterruptedError("film GPU batch cancelled")
                phase = torch.exp(2j * math.pi * q[:, index + 1].unsqueeze(0)
                                  * thickness[:, index:index + 1] / waves.unsqueeze(0))
                phase2 = phase * phase
                denominator = 1 + ri[:, index].unsqueeze(0) * r * phase2
                t = ti[:, index].unsqueeze(0) * phase * t / denominator
                r = (ri[:, index].unsqueeze(0) + r * phase2) / denominator
            f0, fe = self.fluxes[p]
            reflected = _squared_magnitude(torch, r)
            transmitted = _squared_magnitude(torch, t) * torch.clamp(fe[selection], min=0) / f0[selection]
            absorption = 1 - reflected - transmitted
            absorption = torch.where(torch.abs(absorption) < 5e-13, 0., absorption)
            channels.append(torch.stack((reflected, transmitted, absorption), dim=2))
        return sum(channels) / len(channels)

    def evaluate(self, thickness_nm, *, chunk_bytes=64 * 1024**2, cancel=None):
        torch = self.torch
        d = np.asarray(thickness_nm, dtype=np.float64)
        layers = self.stack.indices.shape[1] - 2
        if d.ndim != 2 or d.shape[0] == 0 or d.shape[1] != layers or np.any(d < 0) or not np.all(np.isfinite(d)):
            raise ValueError("thickness_nm requires finite nonnegative [candidate,layer] nm")
        if type(chunk_bytes) is not int or chunk_bytes < 1024:
            raise ValueError("chunk_bytes must be integer >= 1024")
        def check():
            if cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()):
                raise InterruptedError("film GPU batch cancelled")
        check()
        result = np.empty((d.shape[0], self.waves.numel(), 3), dtype=np.float64)
        # Conservative scratch convention, excludes separately limited retained
        # output and resident stack. Never use all reported free VRAM.
        cells = max(1, chunk_bytes // 512)
        wave_count = min(self.waves.numel(), cells)
        candidates = max(1, cells // max(1, wave_count))
        upload = forward = download = 0.
        with torch.no_grad():
            for c in range(0, d.shape[0], candidates):
                check()
                start = time.perf_counter()
                dt = torch.tensor(d[c:c + candidates], dtype=torch.float64, device=self.device)
                self.runtime.synchronize(self.device)
                upload += time.perf_counter() - start
                for w in range(0, self.waves.numel(), wave_count):
                    check()
                    start = time.perf_counter()
                    block = self._compute(dt, w, min(w + wave_count, self.waves.numel()), cancel=cancel)
                    if block.device != self.device or block.dtype != torch.float64:
                        raise RuntimeError("GPU spectrum changed device or FP64 dtype")
                    self.runtime.synchronize(self.device)
                    forward += time.perf_counter() - start
                    start = time.perf_counter()
                    actual = block.cpu().numpy()
                    download += time.perf_counter() - start
                    if not np.all(np.isfinite(actual)):
                        raise ValueError("nonfinite GPU spectrum; whole batch discarded")
                    result[c:c + dt.shape[0], w:w + actual.shape[1]] = actual
                    del block
                del dt
        check()
        self.last_timing = {"thickness_upload_seconds": upload, "forward_seconds": forward,
                            "download_seconds": download,
                            "output_device": str(self.device), "kernel_mode": "eager",
                            "explicit_compile_seconds": 0.,
                            "peak_allocated_bytes": int(self.runtime.max_memory_allocated(self.device))}
        return result

    def verify(self, thickness_nm, *, chunk_bytes=64 * 1024**2, cancel=None):
        """Stack-local real numerical gate, before any GPU output is selected."""
        d = np.asarray(thickness_nm, dtype=np.float64)
        probes = np.concatenate((d[:2], np.zeros((1, d.shape[1]))), axis=0)
        cpu = self.stack.evaluate(probes, chunk_bytes=chunk_bytes, cancel=cancel)
        gpu = self.evaluate(probes, chunk_bytes=chunk_bytes, cancel=cancel)
        error = float(np.max(np.abs(cpu - gpu)))
        if not np.allclose(cpu, gpu, rtol=2e-12, atol=2e-12):
            raise RuntimeError(f"GPU spectrum CPU comparison failed: max absolute error {error}")
        self.verified = True
        return error


class CudaCoherentBatch(TorchCoherentBatch):
    """Compatibility wrapper for explicitly NVIDIA-only consumers."""

    def __init__(self, stack):
        super().__init__(stack, backend="cuda")
