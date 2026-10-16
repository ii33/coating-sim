"""Capture portable, typed design graphs without writing temporary designs.

Only ``freeze_process`` reads the filesystem, through the service's path policy.
The runtime is always rebuilt from the same captured JSON graph, never from a
second read or from the optional cached runtime supplied by an asset package.
Snapshots are detached JSON-compatible dictionaries; callers own their copies.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .data_files import MAX_FILE_BYTES
from .design_files import DESIGN_SUFFIXES, validate_design_member_path, validate_design_path
from .machine_profiles import validate_machine_profile, validate_mask_profile
from .process_profiles import validate_process_profile
from .source_profiles import validate_source_profile
from .substrate_profiles import effective_radius_mm, validate_substrate_profile

SNAPSHOT_KIND = "coat_sim.input_snapshot"
SNAPSHOT_VERSION = 1
PROCESS_PATH = "designs/process.json"
_VALIDATORS = {
    "process": validate_process_profile, "machine": validate_machine_profile,
    "source": validate_source_profile, "mask": validate_mask_profile,
    "substrate": validate_substrate_profile,
}


def canonical_json_bytes(data: Any) -> bytes:
    """One canonical UTF-8 encoding for graph hashes and package writers."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(data: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(data)).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"设计 JSON 中参数重复：{key}")
        result[key] = value
    return result


def _portable_path(value: Any, *, reference: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("快照 JSON 路径无效")
    parts = value.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
        prefix + str(i) for prefix in ("COM", "LPT") for i in range(1, 10)}
    if (any(part in ("", ".", "..") or part.endswith((".", " ")) or
            part.split(".", 1)[0].upper() in reserved for part in parts)
            or any(ord(char) < 32 or char in '\\<>:"|?*' for char in value)
            or PurePosixPath(value).suffix.lower() not in DESIGN_SUFFIXES
            or (not reference and (len(parts) < 2 or parts[0] != "designs"))):
        raise ValueError("快照仅允许 designs 内的便携 JSON 路径")
    return value


def _reference(owner: str, value: Any) -> str:
    relative = _portable_path(value, reference=True)
    return _portable_path(str(PurePosixPath(owner).parent / relative))


def _normalize(kind: str, data: Any) -> dict[str, Any]:
    # Match standalone source-file requirements, not the looser draft helper.
    if kind == "source" and (not isinstance(data, Mapping) or
                             data.get("schema_version") != 2):
        raise ValueError("蒸发源文件必须是第 2 版 JSON")
    return _VALIDATORS[kind](deepcopy(data))


def _record(kind: str, path: str, data: dict[str, Any], original_path=None):
    captured = deepcopy(data)
    return {"kind": kind, "path": path, "data": captured,
            "sha256": _sha256(captured), "original_path": original_path}


def freeze_process(service, data: Mapping[str, Any], base_dir) -> dict[str, Any]:
    """Capture a submitted recipe and every dependency once, entirely in RAM.

    ``data`` is the current process draft; ``base_dir`` resolves its references.
    Shared files are read once even when installed in several slots. All reads
    are fresh and restricted by ``service._path``; no resolver cache is used.
    ``original_path`` is provenance, never an input to snapshot reopening.
    """
    base = service._path(base_dir)
    process = service._normalize("process", deepcopy(data))
    files: list[dict[str, Any]] = []
    captured: dict[Path, dict[str, Any]] = {}

    def capture(kind: str, value, owner: Path, portable: str):
        target = service._path(value, base=owner)
        validate_design_path(kind, target)
        previous = captured.get(target)
        if previous is not None:
            if previous["kind"] != kind:
                raise ValueError("同一设计文件不能用作不同类型的依赖")
            return previous
        # Bounded read prevents oversized or concurrently growing inputs from
        # being ingested. Each file's captured bytes are parsed only once.
        with target.open("rb") as stream:
            content = stream.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("设计 JSON 文件过大")
        raw = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
        if kind == "source" and (not isinstance(raw, Mapping) or
                                 raw.get("schema_version") != 2):
            raise ValueError("蒸发源文件必须是第 2 版 JSON")
        normalized = service._normalize(kind, raw)
        record = _record(kind, portable, normalized, str(target))
        captured[target] = record
        files.append(record)
        return record

    machine = capture("machine", process["machine_file"], base, "designs/machine.json")
    substrate = capture("substrate", process["substrate_file"], base, "designs/substrate.json")
    machine_dir = Path(machine["original_path"]).parent
    for kind, collection in (("source", "sources"), ("mask", "masks")):
        for index, entry in enumerate(machine["data"][collection], 1):
            item = capture(kind, entry["file"], machine_dir,
                           f"designs/{kind}-{index:02d}.json")
            entry["file"] = PurePosixPath(item["path"]).name
    machine["sha256"] = _sha256(machine["data"])
    process["machine_file"] = PurePosixPath(machine["path"]).name
    process["substrate_file"] = PurePosixPath(substrate["path"]).name
    # A draft has no known source filename. Do not invent provenance from its
    # dependency base; callers with an actual saved recipe can supply it later.
    files.insert(0, _record("process", PROCESS_PATH, process))
    snapshot = {"kind": SNAPSHOT_KIND, "version": SNAPSHOT_VERSION,
                "process_path": PROCESS_PATH, "files": files}
    snapshot["runtime"] = runtime_from_snapshot(snapshot)
    return snapshot


def _validated_records(snapshot: Mapping[str, Any]):
    if (not isinstance(snapshot, Mapping) or snapshot.get("kind") != SNAPSHOT_KIND
            or type(snapshot.get("version")) is not int
            or snapshot["version"] != SNAPSHOT_VERSION):
        raise ValueError("输入快照类型或版本无效")
    if set(snapshot) - {"kind", "version", "process_path", "files", "runtime", "parameters", "created_at", "manifest_file"}:
        raise ValueError("输入快照包含不支持的字段")
    # Audit-only provenance, never read to reconstruct a design graph.
    for key in ("created_at", "manifest_file"):
        if key in snapshot and not isinstance(snapshot[key], str):
            raise ValueError("快照审计信息须为字符串")
    if "parameters" in snapshot:
        if not isinstance(snapshot["parameters"], dict):
            raise ValueError("输入快照计算参数须为 JSON 对象")
        canonical_json_bytes(snapshot["parameters"])
    process_path = _portable_path(snapshot.get("process_path"))
    records = snapshot.get("files")
    if not isinstance(records, list) or len(records) < 3:
        raise ValueError("输入快照设计文件数量无效")
    result = {}
    names = set()
    for record in records:
        if not isinstance(record, Mapping) or set(record) - {
                "kind", "path", "data", "sha256", "original_path"}:
            raise ValueError("输入快照设计记录无效")
        path = _portable_path(record.get("path"))
        if path.casefold() in names:
            raise ValueError("输入快照包含重复路径")
        names.add(path.casefold())
        kind = record.get("kind")
        if not isinstance(kind, str) or kind not in _VALIDATORS:
            raise ValueError("输入快照设计类型无效")
        validate_design_member_path(kind, path)
        original = record.get("original_path")
        if original is not None and (not isinstance(original, str) or len(original) > 4096):
            raise ValueError("输入快照来源路径无效")
        data = record.get("data")
        if not isinstance(data, dict):
            raise ValueError("输入快照设计数据须为 JSON 对象")
        digest = record.get("sha256")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or _sha256(data) != digest):
            raise ValueError(f"输入快照校验和不匹配：{path}")
        result[path] = {"kind": kind, "data": _normalize(kind, data)}
    return process_path, result


def runtime_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a complete graph and resolve a fresh runtime without any I/O.

    File kinds, canonical hashes, reference targets, source/monitor/mask slots,
    and installed board heights are checked with the existing profile rules.
    An embedded ``runtime`` is deliberately ignored, including its paths.
    """
    process_path, records = _validated_records(snapshot)
    used = set()

    def load(path: str, expected: str):
        record = records.get(path)
        if record is None or record["kind"] != expected:
            raise ValueError(f"输入快照缺少 {expected} 依赖或类型不匹配：{path}")
        used.add(path)
        return deepcopy(record["data"])

    profile = load(process_path, "process")
    machine_path = _reference(process_path, profile["machine_file"])
    substrate_path = _reference(process_path, profile["substrate_file"])
    machine = load(machine_path, "machine")
    substrate = load(substrate_path, "substrate")
    for entry in machine["sources"]:
        entry["profile"] = load(_reference(machine_path, entry["file"]), "source")
    for entry in machine["masks"]:
        entry["profile"] = validate_mask_profile(
            load(_reference(machine_path, entry["file"]), "mask"),
            substrate_z_mm=machine["substrate_z_mm"])
    for index, step in enumerate(profile["steps"], 1):
        step.setdefault("mask_slots", list(range(len(machine["masks"]))))
        if step["source_slot"] > len(machine["sources"]):
            raise ValueError(f"steps[{index}].source_slot: 机器没有该源位次")
        if any(slot >= len(machine["masks"]) for slot in step["mask_slots"]):
            raise ValueError(f"steps[{index}].mask_slots: 机器没有该修正板位次")
        if (step["target_mode"] == "monitor" and
                step["monitor_slot"] > len(machine["monitors"])):
            raise ValueError(f"steps[{index}].monitor_slot: 机器没有该监测器位次")
    if used != set(records):
        raise ValueError("输入快照含有未被工艺引用的设计文件")
    machine["substrate_profile"] = deepcopy(substrate)
    machine["substrate_radius_mm"] = effective_radius_mm(substrate)
    return {"profile": profile, "machine_path": machine_path,
            "substrate_path": substrate_path, "substrate_profile": substrate,
            "machine": machine, "steps": profile["steps"], "errors": []}


def candidate_snapshot(snapshot: Mapping[str, Any],
                       candidate_masks: Mapping[int, dict[str, Any]],
                       process_steps) -> dict[str, Any]:
    """Return a detached candidate graph, preserving every unselected board.

    New records avoid changing a fixed slot that shares the selected slot's
    original mask file. Replaced, now-unreferenced records are removed. Nothing
    is saved to disk, and the submitted snapshot is never modified.
    """
    runtime_from_snapshot(snapshot)
    if not isinstance(candidate_masks, Mapping) or any(
            type(slot) is not int or slot < 0 for slot in candidate_masks):
        raise ValueError("候选修正板须为 0 开始的有效板位映射")
    result = deepcopy(dict(snapshot))
    records = {record["path"]: record for record in result["files"]}
    process = records[result["process_path"]]
    machine_path = _reference(result["process_path"], process["data"]["machine_file"])
    machine = records[machine_path]
    masks = machine["data"]["masks"]
    old_mask_paths = {_reference(machine_path, entry["file"]) for entry in masks}
    for slot, candidate in sorted(candidate_masks.items()):
        if slot > len(masks):
            raise ValueError("新增修正板须从下一可用板位依次安装")
        data = validate_mask_profile(deepcopy(candidate),
                                     substrate_z_mm=machine["data"]["substrate_z_mm"])
        stem = f"mask-candidate-{slot + 1:02d}"
        filename = stem + ".json"
        number = 1
        while _reference(machine_path, filename).casefold() in {
                path.casefold() for path in records}:
            number += 1
            filename = f"{stem}-{number}.json"
        path = _reference(machine_path, filename)
        record = _record("mask", path, data)
        records[path] = record
        result["files"].append(record)
        entry = {"file": filename}
        if slot == len(masks):
            masks.append(entry)
        else:
            masks[slot] = entry
    active_mask_paths = {_reference(machine_path, entry["file"]) for entry in masks}
    removed = old_mask_paths - active_mask_paths
    result["files"] = [record for record in result["files"] if record["path"] not in removed]
    process["data"]["steps"] = deepcopy(process_steps)
    process["data"] = validate_process_profile(process["data"])
    machine["data"] = validate_machine_profile(machine["data"])
    process["sha256"] = _sha256(process["data"])
    machine["sha256"] = _sha256(machine["data"])
    result["runtime"] = runtime_from_snapshot(result)
    return result
