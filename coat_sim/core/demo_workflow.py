"""A deterministic, explicitly illustrative source/substrate/machine/recipe set.

The normal core validators and saves are reused; this is no new physics model.
Call under a per-request process lock, then submit through the ordinary job API.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
from uuid import uuid4

from .api import ServiceError
from .atomic_io import replace_with_retry
from .data_files import data_path, file_info, DESIGN_FOLDERS
from .schemas import design_data
from .design_files import design_extension


def _atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def workflow_key(client_request_id: str) -> str:
    if not isinstance(client_request_id, str) or not 1 <= len(client_request_id.strip()) <= 128:
        raise ServiceError("invalid_request_id", "client_request_id 须为 1–128 字符")
    return hashlib.sha256(client_request_id.encode("utf-8")).hexdigest()[:32]


def prepare_demo_workflow(service, client_request_id: str, specification: dict) -> dict:
    """Create/recover only owned, unchanged files. Never overwrite another design."""
    key = workflow_key(client_request_id)
    name = specification["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ServiceError("invalid_name", "演示方案名称须为 1–80 字符")
    thickness, rate = specification["target_thickness_nm"], specification["rate_nm_s"]
    for value, label in ((thickness, "目标厚度"), (rate, "沉积速率")):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ServiceError("invalid_demo_parameter", f"{label}须为有限正数")
    if thickness > 1e9 or thickness / rate > 86400:
        raise ServiceError("invalid_demo_parameter", "厚度须≤1e9 nm，厚度/速率推导时长须≤86400 s")
    for field, low, high in (("grid_size", 21, None), ("time_steps", 1, None)):
        value = specification[field]
        if type(value) is not int or value < low or (high is not None and value > high):
            raise ServiceError("invalid_demo_parameter", f"演示 {field} 须为不少于 {low}" + (f"、不大于 {high}" if high is not None else "") + " 的整数")
    if type(specification["fast_cycle"]) is not bool or type(specification["submit"]) is not bool:
        raise ServiceError("invalid_demo_parameter", "fast_cycle 与 submit 须为布尔值")
    path = data_path(f"tmp/workflows/{key}.json", root_allowed=False)
    legacy = data_path(f"results/workflows/{key}.json", root_allowed=False)
    if not path.exists() and legacy.exists():
        path = legacy
    fingerprint = hashlib.sha256(json.dumps(specification, sort_keys=True, ensure_ascii=False,
                                             allow_nan=False).encode("utf-8")).hexdigest()
    paths = {kind: data_path(f"tmp/workflows/{key}/{kind}{design_extension(kind)}", root_allowed=False)
             for kind in ("source", "substrate", "machine", "process")}
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        paths = {kind: data_path(value, root_allowed=False) for kind, value in previous["paths"].items()}
    note = "本项目演示约定；用于展示软件计算链路，未经过设备标定，不代表生产或加工方案。"
    documents = {
        "source": {"schema_version": 2, "kind": "point", "device_type": "custom",
                   "source_exponent": 1.0, "description": note},
        "substrate": {"schema_version": 1, "name": name + " · 平面圆形基片",
                      "outline": {"kind": "circle", "radius_mm": 150.0},
                      "surface": {"kind": "flat"}, "description": note},
        "machine": {"schema_version": 5, "carrier_x_mm": 0.0, "carrier_y_mm": 0.0,
                    "planet_offset_mm": 312.0, "planet_tilt_deg": 0.0, "substrate_z_mm": 1000.0,
                    "sun_teeth": 120, "planet_teeth": 36, "gear_module_mm": 4.0,
                    "sources": [{"file": service.relative_reference(paths["machine"], paths["source"]),
                                 "x_mm": 300.0, "y_mm": 0.0, "z_mm": 0.0}],
                    "masks": [], "monitors": [], "description": note},
        "process": {"schema_version": 6, "name": name,
                    "machine_file": service.relative_reference(paths["process"], paths["machine"]),
                    "substrate_file": service.relative_reference(paths["process"], paths["substrate"]),
                    "steps": [{"source_slot": 1, "target_nm": float(thickness),
                               "rate_nm_s": float(rate), "target_mode": "mean", "mask_slots": []}],
                    "description": note},
    }
    documents = {kind: design_data(kind, data) for kind, data in documents.items()}
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("client_request_id") != client_request_id or manifest.get("fingerprint") != fingerprint:
            raise ServiceError("request_conflict", "相同 client_request_id 的演示参数不能更改")
    else:
        manifest = {"client_request_id": client_request_id, "fingerprint": fingerprint,
                    "state": "preparing", "specification": deepcopy(specification),
                    "paths": {kind: str(value) for kind, value in paths.items()}}
        _atomic_json(path, manifest)
    # Check previously written dependencies before resuming a partial save.
    for kind, target in paths.items():
        if target.exists():
            current = service.load(kind, target)
            if current.data != documents[kind]:
                raise ServiceError("workflow_file_changed", f"演示{kind}文件已被修改；请使用新请求 ID")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            service.save(kind, target, documents[kind], overwrite=False)
    validated = service.validate("process", path=paths["process"])
    if not validated.valid:
        raise ServiceError("invalid_workflow", "演示工艺依赖校验失败：" + "；".join(validated.errors))
    manifest["state"] = "ready"
    manifest["manifest_file"] = str(path)
    manifest["settings"] = {"carrier_rpm": 15.0, "grid_size": specification["grid_size"],
                            "time_steps": specification["time_steps"]}
    manifest["defaults"] = {"machine": documents["machine"], "substrate": documents["substrate"],
                            "source": documents["source"], "process": documents["process"],
                            "warning": note}
    manifest["files"] = [file_info(target) for target in paths.values()]
    _atomic_json(path, manifest)
    return manifest


def record_workflow_job(manifest: dict, job: dict) -> dict:
    manifest = deepcopy(manifest)
    manifest["job"] = deepcopy(job)
    _atomic_json(data_path(manifest["manifest_file"], root_allowed=False), manifest)
    return manifest
