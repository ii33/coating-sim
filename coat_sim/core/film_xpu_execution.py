"""Narrow, frozen desktop XPU route; no silent CPU forward fallback."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from importlib import import_module
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping
from uuid import UUID

from .film_performance import (
    CoherentBatchEvaluator,
    FrozenFilmEvaluator,
    PerformanceOptions,
    available_cpu_workers,
)

XPU_KIND = "intel_xpu_fp64_v1"
CPU_KIND = "film_cpu_v1"
CAPABILITY_VERSION = 1
_MARKER = "__COAT_FILM_XPU__="


def device_uuid(properties: Any) -> str:
    value = properties.uuid
    if isinstance(value, (bytes, bytearray)):
        return str(UUID(bytes=bytes(value)))
    return str(UUID(str(value)))


def validate_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("film execution device must be a frozen mapping")
    result = deepcopy(dict(value))
    if result == {"kind": CPU_KIND}:
        return result
    if set(result) == {"kind", "cpu_workers", "cpu_parallel_axis"} and result["kind"] == CPU_KIND:
        if type(result["cpu_workers"]) is not int or not 1 <= result["cpu_workers"] <= 1024:
            raise ValueError("CPU worker count must be integer from 1 to 1024")
        if result["cpu_parallel_axis"] not in {"auto", "candidates", "wavelengths"}:
            raise ValueError("unknown CPU parallel axis")
        return result
    if (
        set(result) != {"kind", "index", "uuid", "name", "torch_version"}
        or result.get("kind") != XPU_KIND
    ):
        raise ValueError("unknown film execution device fields")
    if type(result["index"]) is not int or not 0 <= result["index"] < 64:
        raise ValueError("XPU device index must be integer from 0 to 63")
    if not isinstance(result["uuid"], str) or str(UUID(result["uuid"])) != result["uuid"]:
        raise ValueError("XPU device UUID must be canonical")
    for field in ("name", "torch_version"):
        if not isinstance(result[field], str) or not 1 <= len(result[field]) <= 160:
            raise ValueError("invalid XPU device identity")
    if "intel" not in result["name"].lower():
        raise ValueError("this desktop route requires an Intel device")
    return result


def validate_request_domain(request: Any) -> None:
    binding = validate_binding(request.execution_device)
    if request.execution_mode != "deferred_batch" or request.solver != "differential_evolution":
        raise ValueError("explicit film device requires deferred_batch differential_evolution")
    if binding["kind"] == CPU_KIND:
        return
    design = request.design_snapshot
    if (
        design.get("first_surface", "front") != "front"
        or design["substrate"]["sides"] != "single"
        or design.get("back_layers")
    ):
        raise ValueError(
            "Intel XPU v1 requires front incidence and a single-sided semi-infinite substrate"
        )
    if len(request.objectives) != 1 or len(request.objectives[0].angles_deg) != 1:
        raise ValueError("Intel XPU v1 requires one objective and one fixed angle")
    if any(item.side != "front" for item in request.variables):
        raise ValueError("Intel XPU v1 supports front-layer thickness variables only")


def verify_live_binding(binding: Mapping[str, Any], *, select: bool = False) -> Any:
    value = validate_binding(binding)
    torch: Any = import_module("torch")
    runtime = torch.xpu
    if not runtime.is_available() or value["index"] >= runtime.device_count():
        raise RuntimeError("所选 Intel XPU 当前不可用；未回退 CPU")
    properties = runtime.get_device_properties(value["index"])
    if (
        device_uuid(properties) != value["uuid"]
        or properties.name != value["name"]
        or str(torch.__version__) != value["torch_version"]
        or not properties.has_fp64
    ):
        raise RuntimeError("Intel XPU UUID、运行时或 FP64 能力已改变；请重新检测，未回退 CPU")
    if select:
        runtime.set_device(value["index"])
    elif runtime.current_device() != value["index"]:
        raise RuntimeError("XPU 当前设备与冻结索引不一致；未回退 CPU")
    return torch


@dataclass(frozen=True)
class StrictXpuOptions(PerformanceOptions):
    execution_device: Mapping[str, Any] | None = None

    def __post_init__(self):
        super().__post_init__()
        binding = validate_binding(self.execution_device)
        if self.backend != "xpu" or binding["kind"] != XPU_KIND:
            raise ValueError("strict XPU options require an XPU binding")
        object.__setattr__(self, "execution_device", binding)


def options_for_device(binding: Mapping[str, Any]) -> PerformanceOptions:
    value = validate_binding(binding)
    if value["kind"] == CPU_KIND:
        workers = value.get("cpu_workers", 1)
        if workers > available_cpu_workers():
            raise ValueError("frozen CPU worker count exceeds currently available process CPUs")
        return PerformanceOptions(
            backend="cpu",
            cpu_workers=workers,
            cpu_parallel_axis=value.get("cpu_parallel_axis", "auto"),
        )
    verify_live_binding(value, select=True)
    return StrictXpuOptions(backend="xpu", execution_device=value)


def execution_summary(binding: Mapping[str, Any]) -> dict[str, Any]:
    value = validate_binding(binding)
    return {
        "mode": "deferred_batch",
        "forward_backend": "xpu" if value["kind"] == XPU_KIND else "cpu",
        "device": value,
        "scoring_backend": "cpu",
        "independent_validation_backend": "cpu",
        "cpu_forward_fallback": False,
    }


class _StrictXpuBatch(CoherentBatchEvaluator):
    def _evaluate(self, thickness_nm, *, cancel=None):
        if not isinstance(self.options, StrictXpuOptions):
            raise TypeError("strict XPU evaluator requires frozen device options")
        binding = validate_binding(self.options.execution_device)
        verify_live_binding(binding)
        values = super()._evaluate(thickness_nm, cancel=cancel)
        if (
            self.last_metadata.get("backend") != "gpu"
            or self.last_metadata.get("gpu_runtime") != "xpu"
        ):
            raise RuntimeError("XPU 批量前向未返回 XPU；拒绝 CPU 替代")
        self.last_metadata["device_uuid"] = binding["uuid"]
        return values

    def _cpu(self, _d, _workers, _cancel):
        raise RuntimeError("Intel XPU 前向失败，未回退 CPU：" + str(self._gpu_failure))


def make_strict_evaluator(frozen, settings, options: StrictXpuOptions) -> FrozenFilmEvaluator:
    from .film_optics_gpu import gpu_domain_reason

    evaluator = FrozenFilmEvaluator(frozen, settings, options=options)
    batch = evaluator._batch
    if batch is None or evaluator.resolved_mode != "semi_infinite":
        raise ValueError("Intel XPU v1 不支持该基片模型；请选择 CPU")
    reason = gpu_domain_reason(batch.stack)
    if reason:
        raise ValueError("Intel XPU v1 不支持精确/近临界条件；请选择 CPU：" + reason)
    evaluator._batch = _StrictXpuBatch(batch.stack, options)
    return evaluator


def live_probe() -> list[dict[str, Any]]:
    """Called only inside the explicit bounded probe child."""
    torch: Any = import_module("torch")
    from .film_optics_gpu import verify_gpu_dtype, _squared_magnitude

    if not torch.xpu.is_available():
        raise RuntimeError("当前解释器未提供可用 Intel XPU；需要已有兼容 PyTorch XPU 环境")
    rows = []
    for index in range(min(int(torch.xpu.device_count()), 8)):
        properties = torch.xpu.get_device_properties(index)
        if "intel" not in properties.name.lower() or not properties.has_fp64:
            continue
        torch.xpu.set_device(index)
        device = torch.device("xpu", index)
        verify_gpu_dtype(torch, torch.xpu, device)
        sample = torch.tensor([1 + 2j], dtype=torch.complex128, device=device)
        squared = _squared_magnitude(torch, sample)
        torch.xpu.synchronize(device)
        if squared.dtype != torch.float64 or abs(float(squared.cpu()[0]) - 5.0) > 1e-12:
            raise RuntimeError("XPU FP64 幅值自乘核验失败")
        binding = validate_binding(
            dict(
                kind=XPU_KIND,
                index=index,
                uuid=device_uuid(properties),
                name=str(properties.name),
                torch_version=str(torch.__version__),
            )
        )
        rows.append(
            {
                "id": f"xpu:{index}",
                "label": f"{properties.name} · XPU {index} · FP64",
                "backend": "xpu",
                "available": True,
                "reason": "",
                "physical_id": binding["uuid"],
                "execution_device": binding,
                "support_level": "experimental",
                "support_note": "实验：固定拓扑差分进化、单条件单角度、前表面半无限基片；XPU批量前向，CPU评分/验证；失败不回退CPU。",
            }
        )
    if not rows:
        raise RuntimeError("未检测到有明确 UUID 与 FP64 能力的 Intel XPU")
    return rows


def probe_devices(timeout: float = 15.0) -> list[dict[str, Any]]:
    from coat_sim.agent.processes import ProcessTree
    from coat_sim.project_logging import logged_module_command

    environment = os.environ.copy()
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "env": environment,
        "cwd": str(Path(__file__).resolve().parents[2]),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    child = subprocess.Popen(
        logged_module_command(
            "coat_sim.film_xpu_probe",
            channel="compute-worker",
            project_root=Path(__file__).resolve().parents[2],
            stderr_only=True,
        ),
        **kwargs,
    )
    tree = ProcessTree(child)
    try:
        output, _ = child.communicate(timeout=max(1.0, min(float(timeout), 30.0)))
        if child.returncode or len(output) > 64 * 1024:
            raise RuntimeError("Intel XPU 探测失败或原生退出；未改变 CPU 选择")
        lines = output.decode("utf-8", errors="replace").splitlines()
        row = next(
            (line[len(_MARKER) :] for line in reversed(lines) if line.startswith(_MARKER)), None
        )
        value = json.loads(row or "null")
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise RuntimeError(
                value.get("error", "Intel XPU 探测未返回结果")
                if isinstance(value, dict)
                else "Intel XPU 探测未返回结果"
            )
        rows = value.get("devices")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
            raise RuntimeError("Intel XPU 设备目录无效")
        for item in rows:
            binding = validate_binding(item.get("execution_device"))
            if (
                item.get("id") != f"xpu:{binding['index']}"
                or item.get("physical_id") != binding["uuid"]
            ):
                raise RuntimeError("Intel XPU 设备绑定无效")
        return rows
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Intel XPU 探测超时；未改变 CPU 选择") from exc
    finally:
        tree.close()


def probe_main() -> None:
    try:
        value = {"ok": True, "devices": live_probe()}
    except Exception as exc:
        value = {"ok": False, "error": (type(exc).__name__ + ": " + str(exc))[:300]}
    print(_MARKER + json.dumps(value, ensure_ascii=False), flush=True)
