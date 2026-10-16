"""Registered design-to-result workflow using the ordinary core services.

The contract is declarative JSON. Programs own dependencies, save order,
submission, result reading and export; an agent may only supply the contract.
No new deposition or geometry algorithm is implemented here.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from coat_sim.workflow import StepResult, Workflow, WorkflowNode
from .api import ServiceError
from .data_files import data_path, file_info, DESIGN_FOLDERS
from .design_files import design_extension
from .demo_workflow import _atomic_json, workflow_key
from .schemas import (SourceDesign, SubstrateDesign, MaskDesign, MonitorPlacement,
                      ProcessStep, ProcessEnvironment, SimulationSettings, design_data)
from .process_environment import model_dump_with_environment


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowSource(_Contract):
    design: SourceDesign
    x_mm: FiniteFloat = 300.0
    y_mm: FiniteFloat = 0.0
    z_mm: FiniteFloat = 0.0


class WorkflowGeometry(_Contract):
    carrier_x_mm: FiniteFloat = 0.0
    carrier_y_mm: FiniteFloat = 0.0
    substrate_z_mm: FiniteFloat = Field(default=1000.0, gt=0, le=100000)
    planet_tilt_deg: FiniteFloat = Field(default=0.0, ge=-80, le=80)
    sun_teeth: int = Field(default=120, ge=1, le=1000)
    planet_teeth: int = Field(default=36, ge=1, le=1000)
    gear_module_mm: FiniteFloat = Field(default=4.0, gt=0)


class DesignWorkflowSpec(_Contract):
    """Owned bundle; coordinates mm, layer thickness nm, rates nm/s.

    Source/monitor slots are one-based; mask slots are zero-based. Radius and
    module cannot disagree: center distance is derived by the normal service.
    """
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=20000)
    sources: list[WorkflowSource] = Field(min_length=1, max_length=64)
    masks: list[MaskDesign] = Field(default_factory=list)
    substrate: SubstrateDesign
    machine: WorkflowGeometry = Field(default_factory=WorkflowGeometry)
    monitors: list[MonitorPlacement] = Field(default_factory=list, max_length=64)
    steps: list[ProcessStep] = Field(default_factory=list, max_length=10000)
    film_stack: dict[str, Any] | None = None
    environment: ProcessEnvironment | None = None
    settings: SimulationSettings = Field(default_factory=SimulationSettings)
    backend: Literal["CPU", "自动", "GPU", "DirectML"] = "自动"
    fast_cycle: bool = True
    time_step_s: FiniteFloat | None = Field(default=None, gt=0)
    fast_cycle_residual_percent: FiniteFloat = Field(default=5.0, ge=0, le=100)
    export_pdf: bool = True
    export_grid: bool = True
    export_charts: bool = False

    @model_validator(mode="after")
    def slots(self):
        if bool(self.steps) == (self.film_stack is not None):
            raise ValueError("须且只须提供 steps 显式层表或 film_stack 参数化膜系")
        steps = self.steps
        if self.film_stack is not None:
            from .film_stack import build_film_stack
            generated = build_film_stack({**({"environment": self.environment.model_dump(exclude_unset=True)} if self.environment is not None else {}), **self.film_stack, "name": self.name,
                "machine_file": "machine.cmac", "substrate_file": "substrate.csub"}, machine={
                    "sources": [{"file": f"source-{i + 1}.csrc", "profile": source.design.model_dump(mode="json", exclude_none=True)}
                                for i, source in enumerate(self.sources)],
                    "monitors": self.monitors, "masks": self.masks})
            steps = [ProcessStep.model_validate(step) for step in generated["profile"]["steps"]]
        for step in steps:
            if step.source_slot > len(self.sources):
                raise ValueError("工艺源位次超出 sources 范围")
            if step.monitor_slot is not None and step.monitor_slot > len(self.monitors):
                raise ValueError("工艺监测器位次超出 monitors 范围")
            if step.mask_slots is not None and any(slot >= len(self.masks) for slot in step.mask_slots):
                raise ValueError("工艺修正板位次超出 masks 范围")
        if self.time_step_s is not None and "time_steps" in self.settings.model_fields_set:
            raise ValueError("时间步数与时间步长一次只能指定一个")
        if self.time_step_s is not None or "time_steps" not in self.settings.model_fields_set:
            from .process_simulator import process_timing
            timing = process_timing({"schema_version": 6, "name": self.name,
                "machine_file": "machine.cmac", "substrate_file": "substrate.csub",
                "steps": [model_dump_with_environment(step) for step in steps],
                **({"environment": generated["profile"]["environment"]} if self.film_stack is not None and "environment" in generated["profile"] else
                   ({"environment": self.environment.model_dump(exclude_unset=True)} if self.environment is not None else {}))},
                time_step_s=0.025 if self.time_step_s is None else self.time_step_s)
            self.settings = self.settings.model_copy(update={"time_steps": timing["time_steps"]})
            self.time_step_s = None
        return self


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def prepare_design_bundle(service, client_request_id: str,
                          specification: dict[str, Any]) -> dict[str, Any]:
    """Normalize then save dependencies in order; owned content must match."""
    spec = model_dump_with_environment(DesignWorkflowSpec.model_validate(specification))
    key = workflow_key(client_request_id)
    manifest_path = data_path(f"tmp/workflows/design-{key}.json", root_allowed=False)
    legacy = data_path(f"results/workflows/design-{key}.json", root_allowed=False)
    if not manifest_path.exists() and legacy.exists():
        manifest_path = legacy
    paths = {
        "sources": [data_path(f"tmp/workflows/design-{key}/source-{n+1}.csrc", root_allowed=False)
                    for n in range(len(spec["sources"]))],
        "masks": [data_path(f"tmp/workflows/design-{key}/mask-{n+1}.cmsk", root_allowed=False)
                  for n in range(len(spec["masks"]))],
        **{kind: data_path(f"tmp/workflows/design-{key}/{kind}{design_extension(kind)}", root_allowed=False)
           for kind in ("substrate", "machine", "process")},
    }
    if manifest_path.exists():
        prior = json.loads(manifest_path.read_text(encoding="utf-8"))["paths"]
        paths = {kind: [data_path(p, root_allowed=False) for p in value] if isinstance(value, list)
                 else data_path(value, root_allowed=False) for kind, value in prior.items()}
    geometry = deepcopy(spec["machine"])
    geometry.update(service.convert_gear(geometry["sun_teeth"], geometry["planet_teeth"],
                                         module_mm=geometry["gear_module_mm"]))
    machine = {"schema_version": 5, **geometry, "description": spec["description"],
               "sources": [{"file": service.relative_reference(paths["machine"], path),
                            **{axis: source[axis] for axis in ("x_mm", "y_mm", "z_mm")}}
                           for source, path in zip(spec["sources"], paths["sources"])],
               "masks": [{"file": service.relative_reference(paths["machine"], path)} for path in paths["masks"]],
               "monitors": spec["monitors"]}
    process = {"schema_version": 6, "name": spec["name"], "description": spec["description"],
               "machine_file": service.relative_reference(paths["process"], paths["machine"]),
               "substrate_file": service.relative_reference(paths["process"], paths["substrate"]),
               "steps": spec["steps"], **({"environment": spec["environment"]} if "environment" in spec else {})}
    if spec.get("film_stack") is not None:
        from .film_stack import build_film_stack
        process = build_film_stack({**({"environment": spec["environment"]} if "environment" in spec else {}), **spec["film_stack"], "name": spec["name"],
            "description": spec["description"], "machine_file": process["machine_file"],
            "substrate_file": process["substrate_file"]}, machine={**machine,
                "sources": [{**placement, "profile": source["design"]}
                            for placement, source in zip(machine["sources"], spec["sources"])]})["profile"]
    documents = [*(('source', path, item["design"]) for item, path in zip(spec["sources"], paths["sources"])),
                 *(('mask', path, item) for item, path in zip(spec["masks"], paths["masks"])),
                 ("substrate", paths["substrate"], spec["substrate"]),
                 ("machine", paths["machine"], machine), ("process", paths["process"], process)]
    documents = [(kind, path, design_data(kind, document)) for kind, path, document in documents]
    fingerprint = _fingerprint(spec)
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("client_request_id") != client_request_id or manifest.get("fingerprint") != fingerprint:
            raise ServiceError("request_conflict", "相同请求 ID 的工作流参数不能更改")
    else:
        output_folder = data_path("tmp/workflow-output/" + key, root_allowed=False)
        occupied = [str(path) for path in [*(p for _, p, _ in documents),
                                          output_folder / "grid.json", output_folder / "report.pdf"] if path.exists()]
        if occupied:
            raise ServiceError("workflow_file_collision", "工作流目标已有非本流程文件；请使用新请求 ID", details=tuple(occupied))
        manifest = {"client_request_id": client_request_id, "fingerprint": fingerprint,
                    "specification": spec, "state": "preparing", "manifest_file": str(manifest_path),
                    "paths": {kind: [str(p) for p in value] if isinstance(value, list) else str(value)
                              for kind, value in paths.items()}}
        _atomic_json(manifest_path, manifest)
    for kind, path, document in documents:
        if path.exists():
            if service.load(kind, path).data != document:
                raise ServiceError("workflow_file_changed", "已保存的工作流文件被修改；须使用新请求 ID", str(path))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            service.save(kind, path, document, overwrite=False)
    validation = service.validate("process", path=paths["process"])
    if not validation.valid:
        raise ServiceError("workflow_dependency_failed", "工作流依赖校验失败", details=validation.errors)
    manifest["state"] = "ready"
    manifest["files"] = [file_info(path) for _, path, _ in documents]
    _atomic_json(manifest_path, manifest)
    return manifest


def design_pipeline(service, *, submit: Callable, status: Callable,
                    read_result: Callable, export_pdf: Callable, visualize: Callable | None = None) -> Workflow:
    """Host registers trusted callbacks; checkpoints contain data, never code."""
    async def prepare(context):
        manifest = await asyncio.to_thread(prepare_design_bundle, service,
                                           context.input["client_request_id"], context.input["specification"])
        return StepResult(output=manifest)

    async def submit_node(context):
        manifest = context.outputs["prepare"]
        spec = manifest["specification"]
        job = await asyncio.to_thread(submit, spec["settings"], manifest["paths"]["process"],
                                      backend=spec["backend"], fast_cycle=spec["fast_cycle"],
                                      fast_cycle_residual_percent=spec["fast_cycle_residual_percent"],
                                      client_request_id=context.input["client_request_id"],
                                      estimate_id=context.outputs.get("submit", {}).get("estimate_id"),
                                      session_id=context.input.get("session_id", ""),
                                      request_id=context.input.get("request_id", ""))
        if job.get("state") == "awaiting_confirmation":
            return StepResult(output=job, wait_for_input="等待用户确认估算；确认后自动排队")
        return StepResult(output=job)

    async def await_node(context):
        job = await asyncio.to_thread(status, context.outputs["submit"]["job_id"])
        if job["state"] in ("failed", "cancelled"):
            raise ServiceError("workflow_calculation_failed", f"仿真{job['state']}：{job.get('error') or ''}")
        if job["state"] != "succeeded":
            return StepResult(output=job, wait_for_input="后台仿真运行中；查询作业后推进工作流")
        run_id = job.get("result", {}).get("run_id")
        if not run_id:
            raise ServiceError("workflow_result_missing", "成功作业没有结果 run_id")
        return StepResult(output=job, update={"result_run_id": run_id})

    async def export_node(context):
        prepared = context.outputs["prepare"]
        manifest_path = data_path(prepared["manifest_file"], root_allowed=False)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        spec = manifest["specification"]
        run_id = context.data["result_run_id"]
        metadata = await asyncio.to_thread(read_result, run_id, include_grid=spec["export_grid"])
        folder = data_path("tmp/workflow-output/" + workflow_key(context.input["client_request_id"]), root_allowed=False)
        folder.mkdir(parents=True, exist_ok=True)
        files = list(metadata.get("files", []))

        def export_owned(target, write):
            # Recovery may reuse a verified completed file, never an unknown
            # partial write or a user's edited artifact at the same path.
            saved = manifest.setdefault("exports", {}).get(target.name)
            if target.exists():
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if not saved or saved.get("run_id") != run_id or saved.get("sha256") != digest:
                    raise ServiceError("workflow_export_changed", "导出路径存在未确认或已被修改的文件；请使用新请求 ID", str(target))
            else:
                write()
                manifest["exports"][target.name] = {"run_id": run_id,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
                _atomic_json(manifest_path, manifest)
            return file_info(target)
        if spec["export_grid"]:
            target = folder / "grid.json"
            grid = metadata.pop("grid")
            field = grid.get("field_nm", grid.get("field"))
            chart_data = {"run_id": run_id, "coordinate_unit": "mm", "field_unit": metadata["field_unit"],
                          "x": grid["x"], "y": grid["y"], "field": field}
            files.append(export_owned(target, lambda: _atomic_json(target, chart_data)))
        if spec["export_pdf"]:
            target = folder / "report.pdf"
            files.append(await asyncio.to_thread(export_owned, target,
                lambda: export_pdf(run_id, str(target), overwrite=False)))
        if spec.get("export_charts"):
            if visualize is None:
                raise ServiceError("workflow_visualizer_missing", "工作流未配置标准可视化服务")
            charts = await asyncio.to_thread(visualize, run_id)
            files.extend(charts.get("files", []))
        files = list({item["path"]: item for item in files}.values())
        return StepResult(output={"run_id": run_id, "metadata": metadata, "files": files})

    return Workflow("coat.design_simulation_export", "1", "prepare", (
        WorkflowNode("prepare", prepare, "submit"), WorkflowNode("submit", submit_node, "await"),
        WorkflowNode("await", await_node, "export"), WorkflowNode("export", export_node)), max_steps=2000)


def design_workflow_reply(run) -> dict[str, Any]:
    """Small result for agent use, excluding full designs and field arrays."""
    prepared = run.outputs.get("prepare", {})
    job = run.outputs.get("await", run.outputs.get("submit", {}))
    exported = run.outputs.get("export", {})
    return {"ok": run.status not in ("failed", "cancelled"),
            "workflow_run_id": run.run_id, "workflow_status": run.status,
            "state": "cancelled" if run.status == "cancelled" else job.get("state", prepared.get("state", run.status)),
            "job_id": job.get("job_id"), "estimate_id": job.get("estimate_id"), "job": job, "run_id": exported.get("run_id"),
            "paths": prepared.get("paths", {}), "manifest_file": prepared.get("manifest_file"),
            "files": [*prepared.get("files", []), *exported.get("files", [])],
            "result": exported.get("metadata"), "waiting_for": run.waiting_for,
            "error": run.error, "step_count": run.step_count}
