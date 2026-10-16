"""Independent coating recipes with explicit machine and substrate files."""

from .data_files import METADATA_FIELDS, design_metadata
from .design_files import validate_design_path

from collections.abc import Mapping
from copy import deepcopy
import json
import math
from pathlib import Path

from .process_environment import validate_environment, resolve_environment, environment_report
from .machine_profiles import read_machine_profile, resolve_machine_profile
from .substrate_profiles import (effective_radius_mm, read_substrate_profile)

PROCESS_SCHEMA_VERSION = 7
READABLE_PROCESS_SCHEMA_VERSIONS = (6, PROCESS_SCHEMA_VERSION)
TARGET_MODES = ("mean", "center", "monitor")
TARGET_MODE_LABELS = {
    "mean": "整片平均厚度", "center": "基片中心厚度",
    "monitor": "指定监测器厚度",
}
MAX_PROCESS_STEPS = 10000
MAX_TARGET_NM = 1_000_000_000.0
MAX_STEP_DURATION_S = 86400.0


def _finite_number(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是有限数值")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须是有限数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}必须是有限数值")
    return number


def _positive_slot(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label}必须是从 1 开始的整数位次")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"工艺 JSON 中参数重复：{key}")
        result[key] = value
    return result


def validate_process_profile(data):
    """Validate process-owned fields and normalize readable v6 documents to v7."""
    if not isinstance(data, Mapping):
        raise ValueError("工艺配置必须是 JSON 对象")
    version = data.get("schema_version")
    if isinstance(version, bool) or version not in READABLE_PROCESS_SCHEMA_VERSIONS:
        readable = "、".join(map(str, READABLE_PROCESS_SCHEMA_VERSIONS))
        raise ValueError(f"工艺配置仅支持第 {readable} 版，请新建方案")
    if version == 6 and ("film_design_snapshot" in data or "film_design_import" in data or
                         any(isinstance(step, Mapping) and
                             ("film_layer_ref" in step or "film_design_thickness_nm" in step)
                             for step in data.get("steps", []))):
        raise ValueError("工艺 v6 不包含膜系导入字段；请使用 v7 保存膜系快照")
    extra = set(data) - METADATA_FIELDS - {"schema_version", "name", "machine_file",
                         "substrate_file", "steps", "environment",
                         "film_design_snapshot", "film_design_import"}
    if extra:
        raise ValueError(f"工艺包含不支持的参数：{', '.join(sorted(extra))}")
    name = data.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError("工艺名称必须为 1–100 个字符")
    dependencies = {}
    for field, label in (("machine_file", "机器"),
                         ("substrate_file", "基片")):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"工艺须指定{label}文件 {field}")
        if len(value) > 4096:
            raise ValueError(f"{label}文件路径过长")
        relative = Path(value.strip())
        if relative.drive and not relative.is_absolute():
            raise ValueError(f"{field} 不能使用带盘符的非绝对路径")
        dependencies[field] = value.strip()
    steps = data.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_PROCESS_STEPS:
        raise ValueError(f"工艺必须包含 1–{MAX_PROCESS_STEPS} 层")

    normalized_steps = []
    for index, step in enumerate(steps, 1):
        label = f"第 {index} 层"
        if not isinstance(step, Mapping):
            raise ValueError(f"{label}必须是 JSON 对象")
        extra = set(step) - {"source_slot", "target_nm", "rate_nm_s",
                             "target_mode", "monitor_slot", "mask_slots", "environment",
                             "film_layer_ref", "film_design_thickness_nm"}
        if extra:
            raise ValueError(f"{label}包含不支持的参数：{', '.join(sorted(extra))}")
        source_slot = _positive_slot(step.get("source_slot"), f"{label}蒸发源")
        target_nm = _finite_number(step.get("target_nm"), f"{label}目标厚度")
        if not 0 < target_nm <= MAX_TARGET_NM:
            raise ValueError(f"{label}目标厚度必须在 0–{MAX_TARGET_NM:g} nm 之间")
        rate_nm_s = _finite_number(step.get("rate_nm_s"), f"{label}沉积速率")
        if rate_nm_s <= 0:
            raise ValueError(f"{label}沉积速率必须大于 0 nm/s")
        duration_s = target_nm / rate_nm_s
        if not math.isfinite(duration_s) or not 0 < duration_s <= MAX_STEP_DURATION_S:
            raise ValueError(
                f"{label}按厚度与速率推导的沉积时长必须在 0–{MAX_STEP_DURATION_S:g} s 之间")
        mode = step.get("target_mode")
        if mode not in TARGET_MODES:
            raise ValueError(f"{label}的厚度基准必须是 mean、center 或 monitor")
        normalized = {"source_slot": source_slot, "target_nm": target_nm,
                      "rate_nm_s": rate_nm_s, "target_mode": mode}
        layer_ref = step.get("film_layer_ref")
        design_thickness = step.get("film_design_thickness_nm")
        if (layer_ref is None) != (design_thickness is None):
            raise ValueError(f"{label}膜系来源必须同时提供 film_layer_ref 与 film_design_thickness_nm")
        if layer_ref is not None:
            if not isinstance(layer_ref, Mapping) or set(layer_ref) != {"surface", "layer_id"}:
                raise ValueError(f"{label}.film_layer_ref 必须包含 surface 和 layer_id")
            surface, layer_id = layer_ref.get("surface"), layer_ref.get("layer_id")
            if surface not in ("front", "back") or not isinstance(layer_id, str) or not layer_id.strip():
                raise ValueError(f"{label}.film_layer_ref 面或层 ID 无效")
            physical_nm = _finite_number(design_thickness, f"{label}膜系物理厚度")
            if not 0 < physical_nm <= MAX_TARGET_NM:
                raise ValueError(f"{label}膜系物理厚度须在 0–{MAX_TARGET_NM:g} nm 之间")
            normalized["film_layer_ref"] = {"surface": surface, "layer_id": layer_id.strip()}
            normalized["film_design_thickness_nm"] = physical_nm
        if mode == "monitor":
            normalized["monitor_slot"] = _positive_slot(
                step.get("monitor_slot"), f"{label}监测器")
        elif step.get("monitor_slot") is not None:
            raise ValueError(f"{label}.monitor_slot: 仅 target_mode='monitor' 可指定非空监测器位次；mean/center 请省略或填 null")
        if step.get("mask_slots") is not None:
            slots = step["mask_slots"]
            if not isinstance(slots, list) or any(type(slot) is not int or slot < 0 for slot in slots):
                raise ValueError(f"{label}修正板须为 0 开始的非负整数板位列表")
            if len(slots) != len(set(slots)):
                raise ValueError(f"{label}修正板位次不能重复")
            normalized["mask_slots"] = sorted(slots)
        if step.get("environment") is not None:
            normalized["environment"] = validate_environment(step["environment"])
        environment_report(resolve_environment(data.get("environment"), normalized.get("environment")))
        normalized_steps.append(normalized)
    normalized_profile = {"schema_version": PROCESS_SCHEMA_VERSION,
                          "name": name.strip(), **dependencies,
                          "steps": normalized_steps,
                          **({"environment": validate_environment(data["environment"])} if data.get("environment") is not None else {}),
                          **design_metadata(data)}
    has_snapshot = data.get("film_design_snapshot") is not None
    has_import = data.get("film_design_import") is not None
    has_layer_refs = any("film_layer_ref" in step for step in normalized_steps)
    if has_snapshot != has_import or (has_layer_refs and not has_snapshot):
        raise ValueError("膜系来源必须同时包含冻结快照与导入记录")
    if has_snapshot:
        from .film_process_import import (physical_layer_thickness_nm,
                                          validate_frozen_design_snapshot)
        snapshot = validate_frozen_design_snapshot(data["film_design_snapshot"])
        imported = data["film_design_import"]
        if not isinstance(imported, Mapping) or set(imported) != {
                "snapshot_sha256", "surface", "sequence_order", "layer_ids",
                "unexecuted_surfaces", "source_slots_by_material_id"}:
            raise ValueError("film_design_import 字段无效")
        surface = imported.get("surface")
        if surface not in ("front", "back"):
            raise ValueError("film_design_import.surface 必须是 front 或 back")
        if imported.get("sequence_order") != "substrate_outward":
            raise ValueError("film_design_import.sequence_order 必须是 substrate_outward")
        if imported.get("snapshot_sha256") != snapshot["sha256"]:
            raise ValueError("工艺膜系导入摘要与冻结快照不一致")
        design = snapshot["design"]
        if surface == "back" and design["substrate"]["sides"] != "double":
            raise ValueError("单面基片设计不能导入 back 面")
        selected_layers = design[f"{surface}_layers"]
        layer_by_id = {layer["id"]: layer for layer in selected_layers}
        layer_ids = imported.get("layer_ids")
        if (not isinstance(layer_ids, list) or not layer_ids or
                any(not isinstance(item, str) or item not in layer_by_id for item in layer_ids) or
                len(layer_ids) != len(set(layer_ids))):
            raise ValueError("film_design_import.layer_ids 必须是所选面的唯一设计层 ID 列表")
        expected_ids = [layer["id"] for layer in selected_layers]
        if layer_ids != expected_ids:
            raise ValueError("film_design_import.layer_ids 必须保留所选面的 substrate_outward 顺序")
        other_surface = "back" if surface == "front" else "front"
        if imported.get("unexecuted_surfaces") != [other_surface]:
            raise ValueError("film_design_import 必须明确记录未执行的另一面")
        source_map = imported.get("source_slots_by_material_id")
        expected_materials = {layer["material_id"] for layer in selected_layers}
        if (not isinstance(source_map, Mapping) or set(source_map) != expected_materials or
                any(type(slot) is not int or slot < 1 for slot in source_map.values())):
            raise ValueError("film_design_import.source_slots_by_material_id 必须显式映射所选面的每种材料")
        for step_index, step in enumerate(normalized_steps, 1):
            ref = step.get("film_layer_ref")
            if ref is None:
                continue
            if ref["surface"] != surface or ref["layer_id"] not in layer_by_id:
                raise ValueError(f"第 {step_index} 层的膜系引用不属于已导入面")
            layer = layer_by_id[ref["layer_id"]]
            if step["source_slot"] != source_map[layer["material_id"]]:
                raise ValueError(f"第 {step_index} 层的蒸发源与材料显式映射不一致")
            expected_nm, _n = physical_layer_thickness_nm(snapshot, layer)
            if not math.isclose(step["film_design_thickness_nm"], expected_nm,
                                rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError(f"第 {step_index} 层膜系物理厚度与快照不一致")
        normalized_profile["film_design_snapshot"] = snapshot
        normalized_profile["film_design_import"] = {
            "snapshot_sha256": snapshot["sha256"], "surface": surface,
            "sequence_order": "substrate_outward", "layer_ids": list(layer_ids),
            "unexecuted_surfaces": [other_surface],
            "source_slots_by_material_id": dict(source_map),
        }
    elif has_layer_refs:
        raise ValueError("逐层膜系引用缺少冻结设计快照")
    return normalized_profile


def new_process_profile(machine_file, substrate_file, name="新工艺"):
    return validate_process_profile({
        "schema_version": PROCESS_SCHEMA_VERSION, "name": name,
        "machine_file": str(machine_file),
        "substrate_file": str(substrate_file),
        "steps": [{"source_slot": 1, "target_nm": 100.0,
                   "rate_nm_s": 1.0, "target_mode": "mean"}],
    })


def _dependency_path(value, base_dir):
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(base_dir) / path
    return path.resolve()


def resolve_process_profile(profile, base_dir):
    """Return (runtime, errors); a runtime with errors is display-only."""
    normalized = validate_process_profile(profile)
    machine_path = _dependency_path(normalized["machine_file"], base_dir)
    substrate_path = _dependency_path(normalized["substrate_file"], base_dir)
    runtime = {"profile": normalized, "machine_path": str(machine_path),
               "substrate_path": str(substrate_path),
               "substrate_profile": None, "machine": None,
               "steps": normalized["steps"], "errors": []}
    errors = []
    try:
        machine = read_machine_profile(machine_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        errors.append(f"machine_file: {machine_path}：{exc}")
    else:
        bundle, machine_errors = resolve_machine_profile(machine, machine_path.parent)
        runtime["machine"] = deepcopy(bundle)
        errors.extend(f"machine_file → {issue}" for issue in machine_errors)
    try:
        substrate = read_substrate_profile(substrate_path)
        runtime["substrate_profile"] = substrate
        if runtime["machine"] is not None:
            runtime["machine"]["substrate_profile"] = deepcopy(substrate)
            runtime["machine"]["substrate_radius_mm"] = effective_radius_mm(substrate)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        errors.append(f"substrate_file: {substrate_path}：{exc}")
    bundle = runtime["machine"]
    sources = bundle.get("sources", []) if isinstance(bundle, Mapping) else []
    monitors = bundle.get("monitors", []) if isinstance(bundle, Mapping) else []
    masks = bundle.get("masks", []) if isinstance(bundle, Mapping) else []
    for index, step in enumerate(normalized["steps"], 1):
        step.setdefault("mask_slots", list(range(len(masks))))
        for mask_slot in step["mask_slots"]:
            if mask_slot >= len(masks):
                errors.append(f"steps[{index}].mask_slots: 机器没有板 {mask_slot + 1}；请重选板位")
            elif not isinstance(masks[mask_slot], Mapping) or masks[mask_slot].get("profile") is None:
                errors.append(f"steps[{index}].mask_slots: 板 {mask_slot + 1} 文件无效")
        source_slot = step["source_slot"]
        if source_slot > len(sources):
            errors.append(f"steps[{index}].source_slot: 机器只有 {len(sources)} 个源；请重选源位次")
        elif not isinstance(sources[source_slot - 1], Mapping) or \
                sources[source_slot - 1].get("profile") is None:
            errors.append(f"steps[{index}].source_slot: 第 {source_slot} 位源文件无效")
        if step["target_mode"] == "monitor":
            monitor_slot = step["monitor_slot"]
            if monitor_slot > len(monitors):
                errors.append(f"steps[{index}].monitor_slot: 机器只有 {len(monitors)} 个监测器；请重选位次")
    runtime["errors"] = errors
    return runtime, errors


def source_selection_options(machine):
    """Describe installed saved designs without changing slot or mask identity.

    ``machine`` is an already resolved runtime. Display metadata never belongs
    in a process step and is deliberately regenerated from the source snapshot.
    Reference rates are editable design defaults, not deposition predictions.
    """
    from .source_profiles import validate_source_profile
    sources = machine.get("sources", []) if isinstance(machine, Mapping) else []
    result = []
    for slot, source in enumerate(sources, 1):
        source = source if isinstance(source, Mapping) else {}
        filename = Path(str(source.get("file") or source.get("path") or "")).name
        profile = source.get("profile")
        valid = isinstance(profile, Mapping)
        if valid:
            try:
                profile = validate_source_profile(profile)
            except (ValueError, TypeError):
                valid = False
        profile = profile if valid else {}
        material = profile.get("material") or {}
        material_name = material.get("name", "")
        reference_rate = (profile.get("emission_model") or {}).get("reference_rate_nm_s", 1.0)
        material_label = material_name or ("未指定材料（兼容旧源）" if valid else "源文件无效")
        result.append({"slot": slot, "file": filename, "valid": valid,
                       "kind": profile.get("kind", ""),
                       "device_type": profile.get("device_type", ""),
                       "material_id": material.get("id", ""),
                       "material_name": material_name,
                       "default_rate_nm_s": reference_rate,
                       "default_rate_angstrom_s": reference_rate * 10.0,
                       "label": f"{slot} · {filename or '源文件无效'} · {material_label}"})
    return result


def step_mask_slots(step, machine):
    """Zero-based installed positions; omitted selects all, [] selects none."""
    slots = step.get("mask_slots", list(range(len(machine.get("masks", [])))))
    if not isinstance(slots, list) or any(type(slot) is not int or not 0 <= slot < len(machine.get("masks", [])) for slot in slots):
        raise ValueError("膜层修正板位次已失效；请重新选择")
    if len(slots) != len(set(slots)):
        raise ValueError("膜层修正板位次不能重复")
    return tuple(sorted(slots))


def read_process_profile(path):
    """Read v6/v7 JSON; dependency resolution is an explicit next step."""
    validate_design_path("process", path)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"),
                          object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取工艺文件：{exc}") from exc
    return validate_process_profile(data)


def write_process_profile(path, data):
    """Save only if machine, substrate, and step dependencies resolve."""
    validate_design_path("process", path)
    normalized = validate_process_profile(data)
    runtime, errors = resolve_process_profile(normalized, Path(path).resolve().parent)
    if errors:
        raise ValueError("依赖文件或膜层无效，无法保存：\n" + "\n".join(errors))
    normalized = runtime["profile"]
    Path(path).write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    return normalized
