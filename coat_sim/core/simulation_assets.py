"""Temporary simulation products, portable asset packages and SQLite summaries.

This module transports existing results; it does not calculate new statistics.
Saved packages contain no executable code and are read without ZIP extraction.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import posixpath
import stat
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
from typing import Any
from uuid import uuid4
import zipfile

import numpy as np

from .result_summary import compact_result_metadata, process_summary
from .design_files import (DESIGN_SUFFIXES, design_extension,
                           validate_design_member_path, validate_design_path)
from .atomic_io import replace_with_retry

PACKAGE_KIND = "coat_sim.simulation_asset"
PACKAGE_VERSION = 2
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 256
MAX_DESIGN_BYTES = 8 * 1024 * 1024
MAX_LAYER_FIELD_COUNT = 10_000
MAX_LAYER_FIELD_BYTES = 256 * 1024 * 1024
DESIGN_KINDS = frozenset({"process", "machine", "source", "substrate", "mask"})


def plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, np.generic):
        return plain(value.item())
    if isinstance(value, np.ndarray):
        return {"shape": list(value.shape), "dtype": str(value.dtype)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def asset_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
        raise ValueError("资产名称须为 1–80 个字符，请先向用户确认名称")
    name = value.strip()
    if name.endswith((".", " ")) or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name):
        raise ValueError("资产名称不能含路径分隔符或文件名非法字符")
    if name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
        prefix + str(i) for prefix in ("COM", "LPT") for i in range(1, 10)
    }:
        raise ValueError("资产名称不能使用系统设备名")
    return name


def suggested_name(process: Mapping[str, Any] | None, result: Mapping[str, Any]) -> str:
    title = str((process or {}).get("name", "")).strip()
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", title).strip(" .")
    if title and not re.search(r"[0-9a-f]{16,}", title):
        return title[:60] + " 膜厚仿真"
    count = len((process or {}).get("steps", result.get("step_results", [])))
    return f"{count}层 膜厚仿真" if count else "膜厚仿真结果"


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(plain(value), ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _publish(temporary: Path, destination: Path, overwrite: bool) -> None:
    if overwrite:
        replace_with_retry(temporary, destination)
    else:
        # The temporary is on the destination filesystem. A hard link publishes
        # the complete file atomically and fails if another writer won the name.
        os.link(temporary, destination)
        temporary.unlink()


def _input_signature(metadata: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps(plain(metadata), ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode("utf-8"))
    for key in ("field", "field_nm", "x", "y"):
        if key in result:
            value = np.asarray(result[key])
            if value.dtype.hasobject:
                raise ValueError("结果数组不能含对象")
            digest.update(key.encode())
            digest.update(str((value.dtype.str, value.shape)).encode())
            digest.update(memoryview(np.ascontiguousarray(value)).cast("B"))
    if result.get("layer_field_spool_path") is not None:
        digest.update(str(result["layer_field_manifest"]["fields_sha256"]).encode("ascii"))
    else:
        _, layer_arrays = _layer_field_contract(result)
        for key in sorted(layer_arrays):
            value = np.asarray(layer_arrays[key])
            digest.update(key.encode())
            digest.update(str((value.dtype.str, value.shape)).encode())
            digest.update(memoryview(np.ascontiguousarray(value)).cast("B"))
    return digest.hexdigest()


def _validate_arrays(values: Mapping[str, Any]) -> None:
    field_key = "field_nm" if "field_nm" in values else "field"
    if not all(key in values for key in (field_key, "x", "y")):
        raise ValueError("资产包缺少厚度场或坐标轴")
    x, y, field = (np.asarray(values[key], dtype=float) for key in ("x", "y", field_key))
    if x.ndim != 1 or y.ndim != 1 or field.shape != (len(y), len(x)) or min(len(x), len(y)) < 2:
        raise ValueError("资产包网格与坐标轴形状不一致")
    if not np.isfinite(x).all() or not np.isfinite(y).all() or not np.isfinite(field).any():
        raise ValueError("资产包没有有效坐标或厚度数据")
    if not all(np.all(np.diff(axis) > 0) or np.all(np.diff(axis) < 0) for axis in (x, y)):
        raise ValueError("资产包坐标轴须严格单调")


def _validate_layer_manifest(manifest: Any, *, step_results: Any, field_shape: tuple[int, int],
                              layer_headers: Mapping[str, tuple[tuple[int, ...], np.dtype, int]]) -> list[dict[str, Any]]:
    """Validate the small, explicit index that binds layer maps to NPZ arrays."""
    if not isinstance(manifest, Mapping) or type(manifest.get("schema_version")) is not int or \
            manifest.get("schema_version") != 1 or manifest.get("captured") is not True:
        raise ValueError("逐层场清单版本或捕获标志无效")
    if manifest.get("field_dtype") != "float64" or manifest.get("field_shape") != list(field_shape) or \
            manifest.get("field_units") != "nm":
        raise ValueError("逐层场 dtype、shape 或单位与总场不一致")
    if "complete" in manifest and manifest.get("complete") is not True:
        raise ValueError("逐层场清单尚未完成")
    valid_mask = manifest.get("valid_mask")
    if valid_mask is not None and valid_mask != {
            "semantics": "finite_nodes_are_valid; outside_domain_is_nan",
            "array_source": "layer_field_*"}:
        raise ValueError("逐层场有效域编码不受支持")
    for digest_name in ("dependency_sha256", "fields_sha256"):
        digest = manifest.get(digest_name)
        if digest is not None and (not isinstance(digest, str) or
                                   not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError(f"逐层场 {digest_name} 格式无效")
    coordinates = manifest.get("coordinates")
    if coordinates != {"frame": "substrate_local_xy", "x_array": "x",
                       "y_array": "y", "unit": "mm"}:
        raise ValueError("逐层场坐标系必须是基片局部 XY，坐标单位为 mm")
    if manifest.get("material_coordinate") != {"axis": "substrate_normal", "origin": "original_substrate_front",
                                               "positive_direction": "outward", "unit": "nm"} or \
            manifest.get("layer_value_semantics") != "per_step_growth_increment; cumulative_ordered_sum_is_interface_offset":
        raise ValueError("逐层场材料坐标或前后界面解释无效")
    if manifest.get("sequence_order") != "process_recipe_order" or \
            manifest.get("stack_order") != "first_step_is_substrate_adjacent_then_later_steps_overcoat" or \
            manifest.get("contributor_semantics") != "included_in_total_field":
        raise ValueError("逐层场顺序或总场贡献语义无效")
    contributor = manifest.get("contribution_source_slot")
    if contributor is not None and (type(contributor) is not int or contributor < 1):
        raise ValueError("逐层场贡献源槽位无效")
    rows = manifest.get("fields")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_LAYER_FIELD_COUNT:
        raise ValueError("逐层场数量无效")
    if not isinstance(step_results, list) or len(step_results) != len(rows):
        raise ValueError("逐层场清单与逐步结果数量不一致")
    if len(layer_headers) != len(rows):
        raise ValueError("逐层场数组数量与清单不一致")
    if math.prod(field_shape) * 8 * len(rows) > MAX_LAYER_FIELD_BYTES:
        raise ValueError("逐层场数组总容量超出 256 MiB 限制")

    seen_ids: set[str] = set()
    seen_arrays: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, (row, step_result) in enumerate(zip(rows, step_results), 1):
        if not isinstance(row, Mapping) or not isinstance(step_result, Mapping):
            raise ValueError("逐层场条目必须是 JSON 对象")
        if "completed" in row and row.get("completed") is not True:
            raise ValueError("逐层场清单包含未完成层")
        step_id = row.get("step_id")
        array_key = row.get("array_key")
        if not isinstance(step_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", step_id) or \
                step_id in seen_ids:
            raise ValueError("逐层场 step_id 缺失、重复或格式无效")
        expected_array = f"layer_field_{index:05d}"
        if array_key != expected_array or array_key in seen_arrays:
            raise ValueError("逐层场 NPZ 数组键必须按配方顺序唯一")
        if type(row.get("step_index")) is not int or row.get("step_index") != index or \
                type(step_result.get("step_index")) is not int or step_result.get("step_index") != index or \
                step_result.get("step_id") != step_id:
            raise ValueError("逐层场 ID 或步骤索引与逐步结果不匹配")
        source_slot = row.get("source_slot")
        if type(source_slot) is not int or source_slot < 1 or step_result.get("source_slot") != source_slot:
            raise ValueError("逐层场来源槽位与逐步结果不匹配")
        target = row.get("target_nm")
        if isinstance(target, bool) or not isinstance(target, (int, float)) or \
                not math.isfinite(float(target)) or float(target) <= 0:
            raise ValueError("逐层场目标厚度必须是正的有限 nm 值")
        included = row.get("included_in_total")
        if type(included) is not bool:
            raise ValueError("逐层场必须明确标记是否计入总场")
        if included != (contributor is None or source_slot == contributor):
            raise ValueError("逐层场贡献标记与 contribution_source_slot 不一致")
        layer_id, surface = row.get("layer_id"), row.get("surface")
        if (layer_id is None) != (surface is None) or (layer_id is not None and
                (not isinstance(layer_id, str) or not layer_id.strip() or
                 surface not in ("front", "back"))):
            raise ValueError("逐层场 layer_id/surface 映射无效")
        if ("layer_id" in step_result and step_result.get("layer_id") != layer_id) or \
                ("surface" in step_result and step_result.get("surface") != surface) or \
                ("material_id" in step_result and
                 step_result.get("material_id") != row.get("material_id")):
            raise ValueError("逐层场 layer_id/surface/material_id 与逐步结果不匹配")
        if array_key not in layer_headers:
            raise ValueError("逐层场清单引用的 NPZ 数组缺失")
        shape, dtype, size = layer_headers[array_key]
        if shape != field_shape or dtype.kind != "f" or dtype.itemsize != 8:
            raise ValueError("逐层场数组必须是 shape 一致的 float64 网格")
        if size != math.prod(field_shape) * 8:
            raise ValueError("逐层场数组字节数与 shape/dtype 不一致")
        normalized.append(dict(row))
        seen_ids.add(step_id)
        seen_arrays.add(array_key)
    if seen_arrays != set(layer_headers):
        raise ValueError("NPZ 含清单未引用的逐层场数组")
    return normalized


def _update_layer_field_digest(digest, array_key, values):
    array = np.ascontiguousarray(values, dtype=np.float64)
    digest.update(array_key.encode("ascii"))
    digest.update(json.dumps([array.dtype.str, list(array.shape)],
                             separators=(",", ":")).encode("ascii"))
    digest.update(memoryview(array).cast("B"))


def _layer_field_spool_contract(result: Mapping[str, Any]):
    manifest = result.get("layer_field_manifest")
    if not isinstance(manifest, Mapping) or manifest.get("complete") is not True or \
            manifest.get("captured") is not True or result.get("layer_fields") is not None:
        raise ValueError("流式逐层场必须提供完整清单且不得夹带内存数组")
    if (not isinstance(manifest.get("dependency_sha256"), str) or
            not isinstance(manifest.get("fields_sha256"), str) or
            manifest.get("valid_mask") != {
                "semantics": "finite_nodes_are_valid; outside_domain_is_nan",
                "array_source": "layer_field_*"}):
        raise ValueError("流式逐层场缺少依赖摘要、内容摘要或有效域语义")
    total_key = "field_nm" if "field_nm" in result else "field"
    total = np.asarray(result[total_key])
    step_results = result.get("step_results")
    spool_path = Path(result.get("layer_field_spool_path", ""))
    if spool_path.is_symlink() or not spool_path.is_file():
        raise ValueError("逐层场临时 NPZ 不存在或不是普通文件")
    headers = {}
    try:
        with zipfile.ZipFile(spool_path, "r") as archive:
            members = archive.infolist()
            names = [item.filename for item in members]
            if len(names) != len(set(names)) or any(
                    not re.fullmatch(r"layer_field_[0-9]{5}\.npy", name) for name in names):
                raise ValueError("逐层场临时 NPZ 含重复或未知成员")
            for item in members:
                mode = (item.external_attr >> 16) & 0o170000
                if item.flag_bits & 1 or mode not in (0, stat.S_IFREG):
                    raise ValueError("逐层场临时 NPZ 含加密或非普通成员")
                with archive.open(item) as stream:
                    version = np.lib.format.read_magic(stream)
                    if version == (1, 0):
                        shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
                    elif version == (2, 0):
                        shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
                    else:
                        raise ValueError("逐层场临时 NPZ 数组头版本不受支持")
                    if dtype.hasobject or dtype.fields is not None:
                        raise ValueError("逐层场临时 NPZ 不得含 object 或结构化数组")
                    size = math.prod(shape) * dtype.itemsize
                    if stream.tell() + size != item.file_size:
                        raise ValueError("逐层场临时 NPZ 数组头与成员长度不一致")
                    headers[item.filename[:-4]] = (tuple(shape), dtype, size)
    except (OSError, zipfile.BadZipFile, EOFError) as exc:
        raise ValueError(f"逐层场临时 NPZ 无法读取：{exc}") from exc
    normalized = _validate_layer_manifest(manifest, step_results=step_results,
                                          field_shape=tuple(total.shape), layer_headers=headers)
    if any(row.get("completed") is not True for row in normalized):
        raise ValueError("流式逐层场包含未完成层")
    domain = np.isfinite(total)
    summed = np.zeros(total.shape, dtype=np.float64)
    fields_digest = hashlib.sha256()
    # Validate one layer at a time so the streamed stack never has to be
    # reconstructed in memory during asset preparation.
    with zipfile.ZipFile(spool_path, "r") as archive:
        for row in normalized:
            with archive.open(row["array_key"] + ".npy") as stream:
                layer = np.lib.format.read_array(stream, allow_pickle=False)
            _update_layer_field_digest(fields_digest, row["array_key"], layer)
            if not np.array_equal(np.isfinite(layer), domain) or np.isinf(layer).any():
                raise ValueError("流式层场有效掩码与总厚度场不一致，且不得包含无穷值")
            if row["included_in_total"]:
                summed[domain] += layer[domain]
    if not np.allclose(summed[domain], total[domain], rtol=1e-12, atol=1e-12):
        raise ValueError("总厚度场与逐层场之和不一致")
    if fields_digest.hexdigest() != manifest["fields_sha256"]:
        raise ValueError("流式层场内容摘要与 manifest 不一致")
    return dict(manifest), {}


def _layer_field_contract(result: Mapping[str, Any]):
    """Return a validated manifest and its keyed NPZ arrays, or (None, {})."""
    manifest = result.get("layer_field_manifest")
    fields = result.get("layer_fields")
    if result.get("layer_field_spool_path") is not None:
        if manifest is None:
            raise ValueError("流式逐层场缺少 manifest")
        return _layer_field_spool_contract(result)
    if manifest is None and fields is None:
        return None, {}
    if manifest is None or fields is None or not isinstance(fields, Mapping):
        raise ValueError("逐层场必须同时提供 manifest 与按 step_id 索引的数组")
    total_key = "field_nm" if "field_nm" in result else "field"
    total = np.asarray(result[total_key])
    step_results = result.get("step_results")
    rows = manifest.get("fields") if isinstance(manifest, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("逐层场清单缺少 fields 列表")
    headers = {}
    by_array: dict[str, np.ndarray] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("array_key"), str):
            raise ValueError("逐层场数组映射无效")
        step_id = row.get("step_id")
        if not isinstance(step_id, str) or step_id not in fields:
            raise ValueError("逐层场数组映射缺少 step_id")
        values = np.asarray(fields[step_id])
        headers[row["array_key"]] = (tuple(values.shape), values.dtype, int(values.nbytes))
        by_array[row["array_key"]] = values
    normalized = _validate_layer_manifest(manifest, step_results=step_results,
                                          field_shape=tuple(total.shape), layer_headers=headers)
    if set(fields) != {row["step_id"] for row in normalized}:
        raise ValueError("逐层场数组含多余或缺失的 step_id")
    if manifest.get("fields_sha256") is not None:
        digest = hashlib.sha256()
        for row in normalized:
            _update_layer_field_digest(digest, row["array_key"], by_array[row["array_key"]])
        if digest.hexdigest() != manifest["fields_sha256"]:
            raise ValueError("逐层场内容摘要与清单不一致")
    domain = np.isfinite(total)
    summed = np.zeros(total.shape, dtype=np.float64)
    for row in normalized:
        values = by_array[row["array_key"]]
        if not np.array_equal(np.isfinite(values), domain) or np.isinf(values).any():
            raise ValueError("逐层场有效坐标必须与总场一致，且不得含无穷值")
        if row["included_in_total"]:
            summed[domain] += values[domain]
    if not np.allclose(summed[domain], total[domain], rtol=1e-12, atol=1e-12):
        raise ValueError("计入总场的逐层场之和与总场不一致")
    return dict(manifest), by_array


def _append_npz_members(path: Path, arrays: Mapping[str, Any], metadata_json: str) -> None:
    """Append core result arrays to a spool that already contains layer members."""
    with zipfile.ZipFile(path, mode="a", compression=zipfile.ZIP_DEFLATED,
                         allowZip64=True) as archive:
        names = set(archive.namelist())
        for key, value in arrays.items():
            member = key + ".npy"
            if member in names:
                raise ValueError("逐层场临时 NPZ 已包含总场或坐标数组")
            with archive.open(member, mode="w") as stream:
                np.lib.format.write_array(stream, np.asarray(value), allow_pickle=False)
            names.add(member)
        if "metadata_json.npy" in names:
            raise ValueError("逐层场临时 NPZ 已包含结果元数据")
        with archive.open("metadata_json.npy", mode="w") as stream:
            np.lib.format.write_array(stream, np.asarray(metadata_json), allow_pickle=False)


def _summary(run_id: str, result: Mapping[str, Any], process: Mapping[str, Any] | None,
             elapsed_s: float, requested_device: str) -> dict[str, Any]:
    return {"run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(), "status": "succeeded",
            "name_suggestion": suggested_name(process, result),
            "elapsed_s": float(elapsed_s), "requested_device": requested_device,
            "actual_device": result.get("device"), "algorithm": result.get("algorithm"),
            "process": process_summary(process), "statistics": compact_result_metadata(result)}


def record_simulation_attempt(root, *, status, run_id=None, settings=None,
                               process_path=None, requested_device=None, error=None, elapsed_s=None):
    if status not in ("succeeded", "failed", "cancelled"):
        raise ValueError("仿真尝试状态无效")
    run_id = run_id or uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", str(run_id)):
        raise ValueError("run_id 格式无效")
    if elapsed_s is not None and (not math.isfinite(float(elapsed_s)) or float(elapsed_s) < 0):
        raise ValueError("尝试用时须为有限非负数")
    summary = {"run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(),
               "status": status, "requested_settings": plain(settings),
               "process_path": str(process_path) if process_path is not None else None,
               "requested_device": requested_device, "error": str(error)[:4000] if error else None,
               "elapsed_s": float(elapsed_s) if elapsed_s is not None else None}
    return record_simulation_summary(root, summary)


def record_simulation_summary(root: str | Path, summary: Mapping[str, Any], *,
                              result_file: str | None = None, asset_path: str | None = None) -> dict[str, Any]:
    """Upsert one simulation attempt by run_id, across GUI and MCP processes."""
    database = Path(root) / "data" / "simulation_history.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    run_id = str(summary["run_id"])
    with closing(sqlite3.connect(database, timeout=15)) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS simulations (
            run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, summary_json TEXT NOT NULL,
            result_file TEXT, asset_path TEXT)""")
        db.execute("""INSERT INTO simulations(run_id, created_at, summary_json, result_file, asset_path)
            VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
            summary_json=excluded.summary_json,
            result_file=COALESCE(excluded.result_file,simulations.result_file),
            asset_path=COALESCE(excluded.asset_path,simulations.asset_path)""",
            (run_id, str(summary["created_at"]), json.dumps(plain(summary), ensure_ascii=False, allow_nan=False),
             result_file, asset_path))
    return {"database": str(database), "run_id": run_id}


def list_simulation_summaries(root: str | Path, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError("limit 须为 1–100，offset 须为非负整数")
    database = Path(root) / "data" / "simulation_history.sqlite3"
    if not database.exists():
        return {"records": [], "total": 0, "offset": offset, "has_more": False}
    with closing(sqlite3.connect(database, timeout=15)) as db:
        rows = db.execute("SELECT summary_json,result_file,asset_path FROM simulations ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        total = db.execute("SELECT COUNT(*) FROM simulations").fetchone()[0]
    return {"records": [{**json.loads(row[0]), "result_file": row[1], "asset_path": row[2]} for row in rows],
            "total": total, "offset": offset, "has_more": offset + limit < total}



def _json_bytes(value: Any) -> bytes:
    """Canonical JSON used for frozen-input and package design fingerprints."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")



def _load_json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("JSON 包含重复字段：" + key)
            result[key] = item
        return result
    def invalid_constant(value):
        raise ValueError("JSON 不允许非有限数值：" + value)
    return json.loads(value, object_pairs_hook=unique, parse_constant=invalid_constant)

def _member_path(value: Any) -> str:
    """Reject aliases, traversal and filenames unsafe on either Windows or POSIX."""
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("资产成员路径无效")
    parts = value.split("/")
    if any(part in ("", ".", "..") or part.endswith((".", " ")) for part in parts):
        raise ValueError("资产成员须使用规范相对路径")
    for part in parts:
        asset_name(part)  # Reject device names, control characters, drive syntax.
    return value


def _design_references(kind: str, data: Mapping[str, Any]):
    if kind == "process":
        for field, child in (("machine_file", "machine"), ("substrate_file", "substrate")):
            yield data, field, child
    elif kind == "machine":
        for field, child in (("sources", "source"), ("masks", "mask")):
            for entry in data[field]:
                yield entry, "file", child


def _relative_member(owner: str, reference: Any) -> str:
    if (not isinstance(reference, str) or not reference or "\\" in reference
            or ":" in reference or reference.startswith("/")):
        raise ValueError("设计依赖须为包内相对路径")
    return _member_path(posixpath.normpath(posixpath.join(posixpath.dirname(owner), reference)))


def _validate_design_graph(designs: Mapping[str, dict[str, Any]]) -> None:
    from .schemas import design_data
    for path, row in designs.items():
        _member_path(path)
        if row.get("kind") not in DESIGN_KINDS or not path.startswith("designs/") or PurePosixPath(path).suffix.lower() not in DESIGN_SUFFIXES:
            raise ValueError("资产设计类型或路径无效")
        validate_design_member_path(row["kind"], path)
        design_data(row["kind"], row["data"])
        for owner, field, kind in _design_references(row["kind"], row["data"]):
            target = _relative_member(path, owner[field])
            if target not in designs or designs[target]["kind"] != kind:
                raise ValueError("资产设计依赖缺失或类型不符：" + target)


def portable_input_snapshot(snapshot: Any) -> dict[str, Any] | None:
    """Capture only typed JSON dependencies; never read provenance/original paths.

    Accepts in-memory ``files[{kind,path,data,sha256}]`` snapshots and the two
    older frozen-file contracts. No directory is recursively archived. All
    output design paths are flattened and rebased, avoiding basename collisions.
    """
    from .schemas import design_data
    if snapshot is None:
        return None
    if not isinstance(snapshot, Mapping):
        raise ValueError("输入快照须为对象")
    rows = snapshot.get("files", [])
    if not isinstance(rows, list) or len(rows) > MAX_PACKAGE_ENTRIES - 8:
        raise ValueError("输入快照设计成员数无效")
    in_memory = bool(rows) and all(isinstance(row, Mapping) and "data" in row for row in rows)
    if rows and any("data" in row for row in rows) and not in_memory:
        raise ValueError("不能混合内存与文件输入快照")
    documents: dict[str, dict[str, Any]] = {}
    path_aliases: dict[str, str] = {}
    allowed_files = None
    boundary = None
    if in_memory:
        for row in rows:
            source = _member_path(row["path"])
            if source in documents or source.casefold() in {v.casefold() for v in documents}:
                raise ValueError("输入快照包含重复设计路径")
            if row.get("kind") not in DESIGN_KINDS or PurePosixPath(source).suffix.lower() not in DESIGN_SUFFIXES:
                raise ValueError("输入快照仅支持已知类型 JSON 设计")
            validate_design_member_path(row["kind"], source)
            content = _json_bytes(row["data"])
            if len(content) > MAX_DESIGN_BYTES:
                raise ValueError("设计 JSON 超出容量限制")
            expected = row.get("sha256")
            if expected is not None and hashlib.sha256(content).hexdigest() != expected:
                raise ValueError("输入快照设计校验失败：" + source)
            documents[source] = {"kind": row["kind"], "data": design_data(row["kind"], deepcopy(row["data"]))}
    else:
        if snapshot.get("directory"):
            boundary = Path(snapshot["directory"]).resolve()
        elif snapshot.get("manifest_file"):
            boundary = Path(snapshot["manifest_file"]).resolve().parent
        if rows:
            allowed_files = {}
            for row in rows:
                if not isinstance(row, Mapping) or row.get("kind") not in DESIGN_KINDS or not row.get("snapshot_path"):
                    raise ValueError("旧输入快照缺少类型或冻结路径")
                path = Path(row["snapshot_path"])
                resolved = str(path.resolve())
                if resolved in allowed_files:
                    raise ValueError("输入快照包含重复冻结路径")
                allowed_files[resolved] = row["kind"]
            if boundary is None:
                boundary = Path(os.path.commonpath([str(Path(p).parent) for p in allowed_files]))
        if boundary is None:
            raise ValueError("输入快照缺少冻结目录或内存设计")

        def collect(kind: str, value: Any) -> str:
            path = Path(value)
            if not path.is_absolute():
                path = boundary / path
            if path.is_symlink():
                raise ValueError("冻结设计不能是符号链接")
            path = path.resolve()
            key = str(path)
            validate_design_member_path(kind, path)
            if (not path.is_relative_to(boundary)
                    or not path.is_file() or path.stat().st_size > MAX_DESIGN_BYTES):
                raise ValueError("冻结设计路径或容量无效")
            if allowed_files is not None and allowed_files.get(key) != kind:
                raise ValueError("依赖不在冻结输入清单中或类型不符")
            if key in documents:
                if documents[key]["kind"] != kind:
                    raise ValueError("相同冻结设计不能有多个类型")
                return key
            if len(documents) >= MAX_PACKAGE_ENTRIES - 8:
                raise ValueError("输入设计依赖过多")
            with path.open("rb") as stream:
                payload = stream.read(MAX_DESIGN_BYTES + 1)
            if len(payload) > MAX_DESIGN_BYTES:
                raise ValueError("设计 JSON 超出容量限制")
            data = design_data(kind, _load_json(payload.decode("utf-8")))
            documents[key] = {"kind": kind, "data": data}
            for owner, field, child in _design_references(kind, data):
                owner[field] = collect(child, path.parent / owner[field])
            return key

        if allowed_files is not None:
            for path, kind in allowed_files.items():
                collect(kind, path)
        else:
            for field, kind in (("process_path", "process"), ("machine_path", "machine"),
                                ("substrate_path", "substrate"), ("source_path", "source")):
                if snapshot.get(field):
                    collect(kind, snapshot[field])
            for path in snapshot.get("mask_paths", []):
                collect("mask", path)
    if not documents:
        raise ValueError("输入快照未包含设计")
    for index, (source, row) in enumerate(documents.items()):
        path_aliases[source] = f"designs/{index:04d}-{row['kind']}{design_extension(row['kind'])}"

    def reference(value: Any, kind: str | None = None, owner: str | None = None) -> str:
        key = value
        if in_memory:
            if owner is not None:
                key = _relative_member(owner, value)
            elif key not in path_aliases:
                # Older callers used a logical root basename such as process.json.
                candidates = [p for p in documents if PurePosixPath(p).name == value
                              and (kind is None or documents[p]["kind"] == kind)]
                if len(candidates) == 1:
                    key = candidates[0]
        elif isinstance(value, str):
            candidate = Path(value)
            key = str((candidate if candidate.is_absolute() else boundary / candidate).resolve())
        if key not in path_aliases or (kind is not None and documents[key]["kind"] != kind):
            raise ValueError("输入快照根引用不存在或类型不符")
        return path_aliases[key]

    output = {}
    for source, row in documents.items():
        target = path_aliases[source]
        data = row["data"]
        for owner, field, kind in _design_references(row["kind"], data):
            member = reference(owner[field], kind, source if in_memory else None)
            owner[field] = posixpath.relpath(member, posixpath.dirname(target))
        output[target] = {"path": target, "kind": row["kind"], "data": data,
                          "sha256": hashlib.sha256(_json_bytes(data)).hexdigest()}
    _validate_design_graph(output)
    result = {"kind": "coat_sim.input_snapshot", "version": 1, "files": list(output.values())}
    parameters = snapshot.get("parameters") or {}
    if not isinstance(parameters, Mapping):
        raise ValueError("输入快照计算参数须为对象")
    roots = {}
    for field, kind in (("process_path", "process"), ("machine_path", "machine"),
                        ("substrate_path", "substrate"), ("source_path", "source")):
        value = snapshot.get(field) or parameters.get(field)
        if value:
            roots[field] = reference(value, kind)
    if "process_path" in roots:
        result["process_path"] = roots["process_path"]
    # Keep only declared calculation values; paths, code, credentials and runtime
    # objects are never copied by walking arbitrary snapshot dictionaries.
    safe_keys = {"settings", "backend", "source_slot", "fast_cycle", "fast_cycle_residual_percent",
                 "algorithm", "algorithm_options", "time_step_s"}
    result["parameters"] = {k: plain(v) for k, v in parameters.items() if k in safe_keys}
    if "options" in parameters:
        from .schemas import MaskOptimizationOptions
        supplied = dict(parameters["options"])
        supplied.pop("source_groups", None)
        supplied.pop("new_mask_slots", None)
        result["parameters"]["options"] = MaskOptimizationOptions.model_validate(supplied).model_dump(exclude_none=True)
    result["parameters"].update(roots)
    masks = snapshot.get("mask_paths", parameters.get("mask_paths", []))
    if masks:
        result["parameters"]["mask_paths"] = [reference(value, "mask") for value in masks]
    if result.get("process_path"):
        from .input_snapshot import runtime_from_snapshot
        runtime_from_snapshot(result)
    return result


def _configuration(settings, requested_settings, machine, process, snapshot):
    frozen = portable_input_snapshot(snapshot)
    value = {"settings": plain(settings), "requested_settings": plain(requested_settings),
             "machine": plain(machine), "process": plain(process), "input_snapshot": frozen}
    if frozen:
        for kind in ("machine", "process"):
            rows = [row for row in frozen["files"] if row["kind"] == kind]
            if rows:
                value[kind] = rows[0]["data"]
    return value

def prepare_simulation_asset(root: str | Path, *, result: Mapping[str, Any], settings: Any,
                             machine: Mapping[str, Any] | None, process: Mapping[str, Any] | None,
                             elapsed_s: float, requested_device: str, run_id: str | None = None,
                             requested_settings: Any = None, input_snapshot: Any = None,
                             record_history: bool = True) -> dict[str, Any]:
    """Persist a successful result and its standard heatmap once in data/tmp."""
    from .result_visualization import render_result_visualizations
    run_id = run_id or uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("run_id 格式无效")
    directory = Path(root) / "data" / "tmp" / "simulations" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    _validate_arrays(result)
    spool_path = None
    if result.get("layer_field_spool_path") is not None:
        spool_candidate = Path(result["layer_field_spool_path"])
        if spool_candidate.is_symlink():
            raise ValueError("逐层场临时 NPZ 不得是符号链接")
        spool_path = spool_candidate.resolve(strict=True)
        if not spool_path.is_relative_to(directory.resolve()):
            raise ValueError("逐层场临时 NPZ 必须位于本次 run 目录内")
    _, layer_arrays = _layer_field_contract(result)
    result_arrays = {k: np.asarray(result[k]) for k in ("field", "field_nm", "x", "y") if k in result}
    result_arrays.update(layer_arrays)
    configuration = _configuration(result.get("effective_settings") or settings,
        settings if requested_settings is None else requested_settings, machine, process, input_snapshot)
    summary = _summary(run_id, result, process, elapsed_s, requested_device)
    metadata = {**configuration, "run_id": run_id, "elapsed_s": elapsed_s,
                "requested_device": requested_device,
                "result_metadata": {k: plain(v) for k, v in result.items() if k not in ("field", "field_nm", "x", "y", "layer_fields", "layer_field_spool_path", "_asset")}}
    signature = _input_signature(metadata, result)
    receipt_path = directory / "temporary-asset.json"
    if receipt_path.exists():
        previous = json.loads(receipt_path.read_text(encoding="utf-8"))
        if previous.get("input_signature") != signature:
            raise ValueError("同一 run_id 已有不同的仿真快照，不能覆盖")
        return previous
    npz_path = directory / "result.npz"
    if npz_path.exists():
        with np.load(npz_path, allow_pickle=False) as archived:
            existing = json.loads(str(archived["metadata_json"].item()))
            if any(existing.get(key) != metadata.get(key) for key in
                   ("run_id", "settings", "requested_settings", "machine", "process", "input_snapshot", "elapsed_s", "requested_device", "result_metadata")):
                raise ValueError("已有结果快照与当前参数不一致，不能复用")
            if any(key not in archived or not np.array_equal(archived[key], value, equal_nan=True)
                   for key, value in result_arrays.items()):
                raise ValueError("已有结果数组与当前仿真不一致，不能复用")
    else:
        if spool_path is not None:
            _append_npz_members(spool_path, result_arrays,
                json.dumps(metadata, ensure_ascii=False, allow_nan=False))
            _publish(spool_path, npz_path, False)
        else:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="wb", suffix=".npz", dir=directory, delete=False) as stream:
                    temporary = Path(stream.name)
                    arrays = result_arrays
                    np.savez_compressed(stream, **arrays, metadata_json=np.asarray(json.dumps(metadata, ensure_ascii=False, allow_nan=False)))
            except Exception:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                raise
            try:
                _publish(temporary, npz_path, False)
            finally:
                temporary.unlink(missing_ok=True)
    _atomic_json(directory / "configuration.json", configuration)
    _atomic_json(directory / "result-metadata.json", metadata["result_metadata"])
    _atomic_json(directory / "summary.json", summary)
    if record_history:
        record_simulation_summary(root, summary, result_file=str(npz_path))
    plots = render_result_visualizations(dict(result), dict(process) if process else None, run_id,
                                         directory / "images", charts=["heatmap"])
    # A completed calculation always has one portable temporary bundle. This
    # is not a user-named permanent save and is not indexed as a saved asset.
    bundle_sources = {"result.npz": npz_path, "result-metadata.json": directory / "result-metadata.json"}
    for item in plots["files"]:
        source = Path(item["path"])
        if source.is_symlink() or not source.resolve().is_relative_to(directory.resolve()):
            raise ValueError("临时图像引用无效")
        bundle_sources["images/" + source.name] = source
    temporary_bundle = _write_package(directory / "result.coatasset",
        name=summary["name_suggestion"], run_id=run_id, summary=summary,
        configuration=configuration, sources=bundle_sources, overwrite=True)
    receipt = {"run_id": run_id, "directory": str(directory), "result_file": str(npz_path),
               "temporary_bundle_path": temporary_bundle["asset_path"],
               "files": plots["files"], "summary": summary, "temporary": True,
               "input_signature": signature}
    _atomic_json(directory / "temporary-asset.json", receipt)
    return receipt


def _role_for(path: str) -> str:
    fixed = {"configuration.json": "configuration", "summary.json": "summary",
             "result.npz": "result_arrays", "result-metadata.json": "result_metadata"}
    if path in fixed:
        return fixed[path]
    if path.startswith("designs/") and PurePosixPath(path).suffix.lower() in DESIGN_SUFFIXES:
        return "design"
    if path.startswith("images/"):
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in (".png", ".jpg", ".jpeg", ".webp"):
            return "visualization"
        if suffix == ".csv":
            return "tabular_result"
    raise ValueError("资产包包含未知或可执行的成员类型：" + path)


def _write_package(destination, *, name, run_id, summary, configuration,
                   sources=None, overwrite=False):
    destination = Path(destination)
    if destination.suffix.lower() != ".coatasset":
        raise ValueError("仿真资产包扩展名须为 .coatasset")
    if destination.exists() and not overwrite:
        raise FileExistsError("资产包已存在，覆盖须明确确认")
    if destination.is_symlink():
        raise ValueError("资产包目标不能是符号链接")
    sources = dict(sources or {})
    payloads = {"configuration.json": _json_bytes(configuration), "summary.json": _json_bytes(summary)}
    snapshot = configuration.get("input_snapshot")
    kinds = {}
    if snapshot:
        for row in snapshot["files"]:
            payloads[row["path"]] = _json_bytes(row["data"])
            kinds[row["path"]] = row["kind"]
    names = [*payloads, *sources]
    if len(names) != len(set(name.casefold() for name in names)) or len(names) + 1 > MAX_PACKAGE_ENTRIES:
        raise ValueError("资产包成员数或重复名称无效")
    entries = []
    for member in names:
        _member_path(member)
        role = _role_for(member)
        if member in payloads:
            size = len(payloads[member])
            digest = hashlib.sha256(payloads[member]).hexdigest()
        else:
            size = sources[member].stat().st_size
            digest = _digest(sources[member])
        row = {"path": member, "role": role, "size": size, "sha256": digest}
        if role == "design":
            row["kind"] = kinds[member]
        entries.append(row)
    if sum(row["size"] for row in entries) > MAX_EXPANDED_BYTES:
        raise ValueError("资产包格式安全容量限制：展开内容超过 1 GiB；这不是计算量或内存估算限制")
    manifest = {"kind": PACKAGE_KIND, "schema_version": PACKAGE_VERSION, "name": name,
                "run_id": run_id, "created_at": summary["created_at"], "summary": summary,
                "has_results": "result.npz" in sources, "portable_inputs": bool(snapshot), "entries": entries}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".coatasset", dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", _json_bytes(manifest))
            for member, content in payloads.items():
                archive.writestr(member, content)
            for member, source in sources.items():
                archive.write(source, member)
        # Verify the complete output, including graph closure, before publishing.
        read_simulation_asset(temporary, detail="full")
        _publish(temporary, destination, overwrite)
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "run_id": run_id, "name": name, "asset_path": str(destination),
            "summary": summary, "files": [str(destination)], "has_results": manifest["has_results"],
            "portable_inputs": manifest["portable_inputs"], "design_count": len(kinds)}


def save_simulation_asset(root: str | Path, run_id: str, name: str, *, confirmed: bool,
                          path: str | Path | None = None, overwrite: bool = False,
                          include_results: bool = True) -> dict[str, Any]:
    """Save an existing result and its frozen inputs in the existing package format."""
    if confirmed is not True:
        raise ValueError("请先向用户确认是否保存以及资产名称，再传 confirmed=true")
    name = asset_name(name)
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("run_id 格式无效")
    if type(include_results) is not bool or type(overwrite) is not bool:
        raise ValueError("保存选项须为布尔值")
    directory = Path(root) / "data" / "tmp" / "simulations" / run_id
    receipt = json.loads((directory / "temporary-asset.json").read_text(encoding="utf-8"))
    configuration = json.loads((directory / "configuration.json").read_text(encoding="utf-8"))
    configuration = _configuration(configuration["settings"], configuration.get("requested_settings"),
        configuration.get("machine"), configuration.get("process"), configuration.get("input_snapshot"))
    sources = {}

    def add(source, member):
        source = Path(source)
        if (source.is_symlink() or not source.resolve().is_relative_to(directory.resolve())
                or not source.is_file()):
            raise ValueError("临时资产引用无效")
        _role_for(member)
        if member in sources and sources[member] != source:
            raise ValueError("临时资产成员名称冲突")
        sources[member] = source

    if include_results:
        for filename in ("result.npz", "result-metadata.json"):
            add(directory / filename, filename)
        for info in receipt.get("files", []):
            source = Path(info["path"])
            add(source, "images/" + source.name)
        for source in sorted((directory / "images").glob("*")):
            if source.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".csv"):
                add(source, "images/" + source.name)
    destination = Path(path) if path is not None else Path(root) / "data" / "assets" / (name + ".coatasset")
    saved = _write_package(destination, name=name, run_id=run_id, summary=receipt["summary"],
                           configuration=configuration, sources=sources, overwrite=overwrite)
    record_simulation_summary(root, receipt["summary"], asset_path=str(destination))
    return {**saved, "temporary_source_retained": True}


def save_simulation_asset_copy(root: str | Path, source: str | Path, name: str, *,
                               confirmed: bool, path: str | Path | None = None,
                               overwrite: bool = False, include_results: bool = True) -> dict[str, Any]:
    """Save a verified result bundle without depending on its temporary run directory."""
    if confirmed is not True:
        raise ValueError("需要明确确认是否保存及资产名称，再传 confirmed=true")
    name = asset_name(name)
    if type(include_results) is not bool or type(overwrite) is not bool:
        raise ValueError("保存选项须为布尔值")
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError("队列结果资产包已不存在，不能从任务摘要恢复计算结果")
    content = read_simulation_asset(source, detail="configuration")
    if not content["has_results"]:
        raise ValueError("队列资产包不含计算结果")
    destination = Path(path) if path is not None else Path(root) / "data" / "assets" / (name + ".coatasset")
    if source.resolve() == destination.resolve():
        raise ValueError("结果资产已经位于目标路径")
    destination.parent.mkdir(parents=True, exist_ok=True)
    summary = deepcopy(content["summary"])
    summary["name_suggestion"] = name
    result_roles = {"result_arrays", "result_metadata", "visualization", "tabular_result"}
    sources = {}
    with tempfile.TemporaryDirectory(prefix=".coatasset-copy-", dir=destination.parent) as staging:
        with zipfile.ZipFile(source) as archive:
            manifest, entries, _, _ = _validated_archive(archive)
            if manifest["run_id"] != content["run_id"]:
                raise ValueError("复制期间结果资产标识发生变化")
            for member, entry in entries.items():
                if entry["role"] in {"configuration", "summary", "design"}:
                    continue
                if not include_results and entry["role"] in result_roles:
                    continue
                target = Path(staging).joinpath(*PurePosixPath(member).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as stream, target.open("wb") as output:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
                if _digest(target) != entry["sha256"]:
                    raise ValueError("复制期间结果资产成员校验失败")
                sources[member] = target
        saved = _write_package(destination, name=name, run_id=content["run_id"],
            summary=summary, configuration=content["configuration"], sources=sources,
            overwrite=overwrite)
    record_simulation_summary(root, summary, asset_path=str(destination))
    return {**saved, "copied_from": str(source)}


def save_simulation_input_asset(root: str | Path, *, name: str, settings: Any,
                                input_snapshot: Any, confirmed: bool, path=None,
                                overwrite=False, machine=None, process=None,
                                requested_settings=None) -> dict[str, Any]:
    """Save a portable design/recipe/parameter asset before any calculation."""
    if confirmed is not True:
        raise ValueError("请先确认保存及资产名称")
    if type(overwrite) is not bool:
        raise ValueError("覆盖选项须为布尔值")
    name = asset_name(name)
    configuration = _configuration(settings, settings if requested_settings is None else requested_settings,
                                   machine, process, input_snapshot)
    if not configuration["input_snapshot"]:
        raise ValueError("无结果资产须提供完整冻结输入快照")
    run_id = uuid4().hex
    summary = {"run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(),
               "status": "inputs_only", "name_suggestion": name,
               "process": process_summary(configuration["process"]), "elapsed_s": None,
               "requested_device": configuration["input_snapshot"].get("parameters", {}).get("backend")}
    destination = Path(path) if path is not None else Path(root) / "data" / "assets" / (name + ".coatasset")
    return _write_package(destination, name=name, run_id=run_id, summary=summary,
                          configuration=configuration, overwrite=overwrite)


def _check_asset_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("cancelled")


def _validated_archive(archive, cancel_event=None):
    _check_asset_cancel(cancel_event)
    infos = archive.infolist()
    names = [item.filename for item in infos]
    if (len(infos) > MAX_PACKAGE_ENTRIES
            or len(set(name.casefold() for name in names)) != len(names)):
        raise ValueError("资产包成员数或名称无效")
    if sum(item.file_size for item in infos) > MAX_EXPANDED_BYTES:
        raise ValueError("资产包解压总量超出限额")
    for info in infos:
        _check_asset_cancel(cancel_event)
        _member_path(info.filename)
        mode = (info.external_attr >> 16) & 0o170000
        if info.flag_bits & 1 or mode not in (0, stat.S_IFREG):
            raise ValueError("资产包含加密内容、链接或特殊文件")
        if info.filename != "manifest.json":
            _role_for(info.filename)
    if archive.getinfo("manifest.json").file_size > MAX_DESIGN_BYTES:
        raise ValueError("资产清单超过容量限制")
    manifest = _load_json(archive.read("manifest.json").decode("utf-8"))
    version = manifest.get("schema_version")
    if manifest.get("kind") != PACKAGE_KIND or type(version) is not int or version not in (1, 2):
        raise ValueError("不是受支持的仿真资产包")
    asset_name(manifest["name"])
    if not re.fullmatch(r"[0-9a-f]{32}", str(manifest.get("run_id", ""))):
        raise ValueError("资产包缺少有效标识")
    rows = manifest["entries"]
    if not isinstance(rows, list):
        raise ValueError("资产清单格式无效")
    expected = {}
    for entry in rows:
        _check_asset_cancel(cancel_event)
        name = _member_path(entry["path"])
        if (name in expected or type(entry["size"]) is not int or entry["size"] < 0
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ValueError("资产清单成员格式无效")
        role = _role_for(name)
        if version == 2 and entry.get("role") != role:
            raise ValueError("资产清单成员角色无效")
        if role == "design" and (version == 1 or entry.get("kind") not in DESIGN_KINDS):
            raise ValueError("资产清单设计类型无效")
        expected[name] = {**entry, "role": role}
    if set(expected) | {"manifest.json"} != set(names):
        raise ValueError("资产包清单与内容不一致")
    for name, entry in expected.items():
        _check_asset_cancel(cancel_event)
        if archive.getinfo(name).file_size != entry["size"]:
            raise ValueError("资产包容量校验失败：" + name)
        with archive.open(name) as stream:
            hasher = hashlib.sha256()
            while True:
                _check_asset_cancel(cancel_event)
                block = stream.read(1024 * 1024)
                if not block:
                    break
                hasher.update(block)
            digest = hasher.hexdigest()
        if digest != entry["sha256"]:
            raise ValueError("资产包校验失败：" + name)
    has_results = "result.npz" in expected
    required = {"configuration.json", "summary.json"}
    if version == 1 or has_results:
        required |= {"result.npz", "result-metadata.json"}
    if (not required.issubset(expected) or ("result-metadata.json" in expected) != has_results
            or (version == 2 and (type(manifest.get("has_results")) is not bool
                                or manifest["has_results"] != has_results))):
        raise ValueError("资产包缺少必要成员或结果状态不一致")
    if archive.getinfo("configuration.json").file_size > 32 * MAX_DESIGN_BYTES:
        raise ValueError("资产配置超出容量限制")
    configuration = _load_json(archive.read("configuration.json").decode("utf-8"))
    if not isinstance(configuration, dict) or not all(k in configuration for k in ("settings", "machine", "process")):
        raise ValueError("资产配置缺少必要字段")
    summary = _load_json(archive.read("summary.json").decode("utf-8"))
    if not isinstance(summary, dict) or summary != manifest["summary"] or summary.get("run_id") != manifest["run_id"]:
        raise ValueError("资产摘要与清单不一致")
    designs = {}
    for name, entry in expected.items():
        _check_asset_cancel(cancel_event)
        if entry["role"] == "design":
            if entry["size"] > MAX_DESIGN_BYTES:
                raise ValueError("设计 JSON 超出容量限制")
            data = _load_json(archive.read(name).decode("utf-8"))
            designs[name] = {"path": name, "kind": entry["kind"], "role": "design",
                             "sha256": entry["sha256"], "data": data}
    _validate_design_graph(designs)
    if version == 2:
        if type(manifest.get("portable_inputs")) is not bool or manifest["portable_inputs"] != bool(designs):
            raise ValueError("资产输入状态与设计清单不一致")
        snapshot = configuration.get("input_snapshot")
        if designs:
            if not isinstance(snapshot, dict) or snapshot.get("kind") != "coat_sim.input_snapshot" or snapshot.get("version") != 1:
                raise ValueError("资产输入快照无效")
            records = snapshot["files"]
            if not isinstance(records, list) or len(records) != len(designs):
                raise ValueError("资产输入清单不一致")
            seen = set()
            for row in records:
                path = row["path"]
                if (path in seen or path not in designs or row.get("kind") != designs[path]["kind"]
                        or row.get("data") != designs[path]["data"]
                        or row.get("sha256") != hashlib.sha256(_json_bytes(row["data"])).hexdigest()):
                    raise ValueError("资产输入数据与设计成员不一致")
                seen.add(path)
            for field, kind in (("process_path", "process"), ("machine_path", "machine"),
                                ("source_path", "source"), ("substrate_path", "substrate")):
                value = snapshot.get(field)
                if value is not None and (value not in designs or designs[value]["kind"] != kind):
                    raise ValueError("资产输入根引用无效")
            if snapshot.get("process_path"):
                from .input_snapshot import runtime_from_snapshot
                runtime_from_snapshot(snapshot)
        elif snapshot is not None:
            raise ValueError("资产输入快照没有设计成员")
    return manifest, expected, configuration, designs



def _numeric_payload(archive, *, include_arrays, cancel_event=None):
    """Validate the nested NPZ, including every manifested layer field."""
    _check_asset_cancel(cancel_event)
    with tempfile.TemporaryFile(mode="w+b") as payload:
        with archive.open("result.npz") as source:
            while True:
                _check_asset_cancel(cancel_event)
                block = source.read(1024 * 1024)
                if not block:
                    break
                payload.write(block)
        payload.seek(0)
        with zipfile.ZipFile(payload) as numeric_archive:
            members = numeric_archive.infolist()
            names = [item.filename for item in members]
            core = {"field.npy", "field_nm.npy", "x.npy", "y.npy", "metadata_json.npy"}
            layer_names = {name for name in names if re.fullmatch(r"layer_field_[0-9]{5}\.npy", name)}
            if (len(members) > MAX_LAYER_FIELD_COUNT + 5 or len(names) != len(set(names))
                    or not {"x.npy", "y.npy"}.issubset(names)
                    or not {"field.npy", "field_nm.npy"}.intersection(names)
                    or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES):
                raise ValueError("NPZ 成员数量、名称或展开大小无效")
            headers = {}
            for item in members:
                _check_asset_cancel(cancel_event)
                mode = (item.external_attr >> 16) & 0o170000
                if item.filename not in core | layer_names or item.flag_bits & 1 or mode not in (0, stat.S_IFREG):
                    raise ValueError("NPZ 含未知、加密或非普通文件成员")
                with numeric_archive.open(item) as stream:
                    version = np.lib.format.read_magic(stream)
                    if version == (1, 0):
                        shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
                    elif version == (2, 0):
                        shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
                    else:
                        raise ValueError("不支持的 NPZ 数组头版本")
                    if dtype.hasobject or dtype.fields is not None:
                        raise ValueError("NPZ 不得包含 object 或结构化数组")
                    if item.filename == "metadata_json.npy":
                        if shape != () or dtype.kind not in "US":
                            raise ValueError("NPZ 元数据必须是普通文本标量")
                    elif dtype.kind not in "fiu" or len(shape) not in (1, 2):
                        raise ValueError("NPZ 网格必须是实数轴或二维场")
                    size = math.prod(shape) * dtype.itemsize
                    if size > MAX_EXPANDED_BYTES or stream.tell() + size != item.file_size:
                        raise ValueError("NPZ 数组头声明与成员长度不一致")
                    headers[item.filename] = (tuple(shape), dtype, size)
            x_shape, y_shape = headers["x.npy"][0], headers["y.npy"][0]
            if len(x_shape) != 1 or len(y_shape) != 1 or min(x_shape[0], y_shape[0]) < 2:
                raise ValueError("NPZ 坐标数组 shape 无效")
            field_shape = (y_shape[0], x_shape[0])
            if any(headers[name][0] != field_shape for name in ("field.npy", "field_nm.npy") if name in headers):
                raise ValueError("NPZ 总场与坐标数组 shape 不一致")

            metadata = None
            if "metadata_json.npy" in headers:
                _check_asset_cancel(cancel_event)
                payload.seek(0)
                with np.load(payload, allow_pickle=False) as values:
                    metadata = _load_json(str(values["metadata_json"].item()))
                if not isinstance(metadata, dict):
                    raise ValueError("NPZ 元数据必须是 JSON 对象")
            result_metadata = metadata.get("result_metadata", {}) if metadata else {}
            if not isinstance(result_metadata, Mapping):
                raise ValueError("NPZ result_metadata 必须是 JSON 对象")
            manifest = result_metadata.get("layer_field_manifest")
            layer_headers = {name[:-4]: info for name, info in headers.items()
                             if name.startswith("layer_field_")}
            if manifest is None:
                if layer_headers:
                    raise ValueError("NPZ 含无 manifest 引用的逐层场数组")
                if include_arrays:
                    payload.seek(0)
                    with np.load(payload, allow_pickle=False) as values:
                        result = {key: values[key] for key in ("field", "field_nm", "x", "y") if key in values}
                    _validate_arrays(result)
                    return result
                return None

            rows = _validate_layer_manifest(
                manifest, step_results=result_metadata.get("step_results"),
                field_shape=field_shape, layer_headers=layer_headers)
            field_key = "field_nm" if "field_nm.npy" in headers else "field"
            payload.seek(0)
            with np.load(payload, allow_pickle=False) as values:
                _check_asset_cancel(cancel_event)
                total, x, y = values[field_key], values["x"], values["y"]
                _validate_arrays({field_key: total, "x": x, "y": y})
                if np.isinf(total).any():
                    raise ValueError("逐层场总场不得含无穷值")
                domain = np.isfinite(total)
                summed = np.zeros(total.shape, dtype=np.float64)
                fields_digest = hashlib.sha256()
                layer_values = {}
                for row in rows:
                    _check_asset_cancel(cancel_event)
                    layer = values[row["array_key"]]
                    _update_layer_field_digest(fields_digest, row["array_key"], layer)
                    if not np.array_equal(np.isfinite(layer), domain) or np.isinf(layer).any():
                        raise ValueError("逐层场有效坐标必须与总场一致，且不得含无穷值")
                    if row["included_in_total"]:
                        summed[domain] += layer[domain]
                    if include_arrays:
                        layer_values[row["step_id"]] = layer
                if not np.allclose(summed[domain], total[domain], rtol=1e-12, atol=1e-12):
                    raise ValueError("计入总场的逐层场之和与总场不一致")
                if (manifest.get("fields_sha256") is not None and
                        fields_digest.hexdigest() != manifest["fields_sha256"]):
                    raise ValueError("逐层场内容摘要与清单不一致")
                if include_arrays:
                    result = {key: values[key] for key in ("field", "field_nm", "x", "y") if key in values}
                    result["layer_fields"] = layer_values
                    result["layer_field_manifest"] = dict(manifest)
                    return result
            return None


def _read_simulation_asset(path: str | Path, *, detail: str = "summary", offset: int = 0,
                          limit: int = 20, include_arrays: bool = False,
                          cancel_event=None) -> dict[str, Any]:
    """Validate checksums, roles and dependencies without extracting the package."""
    destination = Path(path)
    if destination.stat().st_size > MAX_PACKAGE_BYTES:
        raise ValueError("资产包超过 512 MiB")
    if detail not in ("summary", "configuration", "layers", "images", "designs", "members", "full"):
        raise ValueError("未知资产包读取视图")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("分页参数无效")
    _check_asset_cancel(cancel_event)
    with zipfile.ZipFile(destination) as archive:
        manifest, expected, configuration, designs = _validated_archive(archive, cancel_event)
        has_results = "result.npz" in expected
        reply = {"ok": True, "asset_path": str(destination), "name": manifest["name"],
                 "schema_version": manifest["schema_version"], "run_id": manifest["run_id"],
                 "summary": manifest["summary"], "detail": detail,
                 "has_results": has_results, "portable_inputs": bool(designs),
                 "design_count": len(designs), "entries": list(expected.values()),
                 "members": list(expected.values()),
                 "available_details": ["configuration", "layers", "images", "designs", "members", "full"]}
        if detail in ("configuration", "designs", "full"):
            reply["configuration"] = configuration
        if detail in ("designs", "full"):
            reply["designs"] = list(designs.values())
        if detail in ("layers", "full"):
            metadata = _load_json(archive.read("result-metadata.json").decode("utf-8")) if has_results else {}
            if not isinstance(metadata, dict):
                raise ValueError("资产结果元数据须为对象")
            if detail == "full":
                reply["result_metadata"] = metadata
            else:
                layers = metadata.get("step_results", [])
                reply.update(layers=[{k: v for k, v in row.items() if k != "effective_settings"}
                                     for row in layers[offset:offset + limit]], offset=offset,
                             total_layers=len(layers), has_more=offset + limit < len(layers))
                if "layer_field_manifest" in metadata:
                    reply["layer_field_manifest"] = metadata["layer_field_manifest"]
        if detail in ("images", "full"):
            reply["images"] = [{"member": name, **info} for name, info in expected.items() if name.startswith("images/")]
        if include_arrays and not has_results:
            reply["arrays"] = {}
        if has_results:
            arrays = _numeric_payload(archive, include_arrays=include_arrays,
                                      cancel_event=cancel_event)
            if include_arrays:
                reply["arrays"] = arrays
        _check_asset_cancel(cancel_event)
        return reply


def read_simulation_asset(path, *, detail="summary", offset=0, limit=20, include_arrays=False,
                          cancel_event=None):
    try:
        return _read_simulation_asset(path, detail=detail, offset=offset, limit=limit,
                                      include_arrays=include_arrays, cancel_event=cancel_event)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, KeyError, TypeError, UnicodeError,
            AttributeError, IndexError, RuntimeError, EOFError, RecursionError) as exc:
        raise ValueError(f"仿真资产包内容损坏或格式无效：{exc}") from exc


def _export_selection(content, members, include_dependencies):
    entries = {row["path"]: row for row in content["entries"]}
    if members is None:
        selected = set(entries)
    else:
        if not isinstance(members, (list, tuple)) or not members:
            raise ValueError("请选择至少一个资产成员")
        selected = {_member_path(member) for member in members}
        if not selected.issubset(entries):
            raise ValueError("导出选择包含不存在的成员")
    designs = {row["path"]: row for row in content["designs"]}
    pending = list(selected)
    while pending:
        member = pending.pop()
        dependencies = set()
        if member == "configuration.json":
            dependencies.update(designs)
        elif member in designs:
            row = designs[member]
            dependencies.update(_relative_member(member, owner[field])
                                for owner, field, _ in _design_references(row["kind"], row["data"]))
        missing = dependencies - selected
        if missing and not include_dependencies:
            raise ValueError("导出选择缺少设计依赖，请同时导出依赖")
        selected.update(missing)
        pending.extend(missing)
    return sorted(selected)


def _export_target(directory, member):
    target = directory.joinpath(*_member_path(member).split("/"))
    if any(part.is_symlink() for part in [target, *target.parents]):
        raise ValueError("导出目标不能包含符号链接")
    if not target.resolve().is_relative_to(directory.resolve()):
        raise ValueError("导出目标超出目标目录")
    if any(parent.exists() and not parent.is_dir() for parent in target.parents):
        raise ValueError("导出目标的父目录与文件冲突")
    if target.exists() and not target.is_file():
        raise ValueError("导出目标与目录或特殊文件冲突")
    return target


def plan_simulation_asset_export(path, destination, *, members=None, include_dependencies=True):
    """Read-only collision preview. UI must confirm before passing overwrite=True."""
    content = read_simulation_asset(path, detail="full")
    selected = _export_selection(content, members, include_dependencies)
    directory = Path(destination).absolute()
    targets = [_export_target(directory, member) for member in selected]
    collisions = [str(target) for target in targets if target.exists()]
    # Case-insensitive aliases would collide when the same export is opened on Windows.
    for target in targets:
        if target.parent.is_dir():
            for sibling in target.parent.iterdir():
                if sibling.name.casefold() == target.name.casefold() and sibling.name != target.name:
                    raise FileExistsError("导出目标存在大小写冲突：" + str(sibling))
    return {"ok": True, "asset_path": str(path), "destination": str(directory),
            "members": selected, "files": [str(target) for target in targets],
            "collisions": collisions, "requires_overwrite_confirmation": bool(collisions)}


def export_simulation_asset(path, destination, *, members=None, include_dependencies=True, overwrite=False):
    """Export selected validated members, preserving paths and dependency closure.

    All collisions are checked before writing. Every file is atomically published;
    a concurrent new collision fails rather than silently replacing that file.
    No ZIP extract/extractall or filesystem path supplied by a package is used.
    """
    if type(overwrite) is not bool or type(include_dependencies) is not bool:
        raise ValueError("导出选项须为布尔值")
    content = read_simulation_asset(path, detail="full")
    plan = plan_simulation_asset_export(path, destination, members=members,
                                       include_dependencies=include_dependencies)
    if plan["collisions"] and not overwrite:
        raise FileExistsError("导出目标已存在，覆盖须明确确认：" + "；".join(plan["collisions"]))
    directory = Path(plan["destination"])
    if overwrite:
        from .schemas import design_data
        designs = {row["path"]: row for row in content["designs"]}
        for member in plan["members"]:
            target = _export_target(directory, member)
            if member in designs and target.exists():
                try:
                    if target.stat().st_size > MAX_DESIGN_BYTES:
                        raise ValueError("已有设计超过容量限制")
                    design_data(designs[member]["kind"], _load_json(target.read_text(encoding="utf-8")))
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    raise ValueError("导出目标无法读取或与设计类型不符，原文件未覆盖；请选择其他目录：" + str(target)) from exc
    # Stage and verify the bytes before publishing any member.
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".coat-export-", dir=directory) as staging:
        with zipfile.ZipFile(path) as archive:
            manifest, entries, _, _ = _validated_archive(archive)
            if manifest["run_id"] != content["run_id"] or entries != {row["path"]: row for row in content["entries"]}:
                raise ValueError("导出期间资产内容发生变化")
            for index, member in enumerate(plan["members"]):
                target = Path(staging) / str(index)
                with archive.open(member) as source, target.open("wb") as stream:
                    shutil.copyfileobj(source, stream, length=1024 * 1024)
                if _digest(target) != entries[member]["sha256"]:
                    raise ValueError("导出成员校验失败")
        for index, member in enumerate(plan["members"]):
            target = _export_target(directory, member)
            target.parent.mkdir(parents=True, exist_ok=True)
            _publish(Path(staging) / str(index), target, overwrite)
    selected = set(plan["members"])
    return {**plan, "exported": True, "designs": [{"kind": row["kind"], "member": row["path"],
            "path": str(directory / row["path"])} for row in content["designs"] if row["path"] in selected]}
