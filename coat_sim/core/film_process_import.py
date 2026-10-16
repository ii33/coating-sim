"""Freeze and map an optical film design into explicit deposition steps.

This module owns the conversion boundary between a film design and a process
recipe. Design layers remain substrate-outward; the user must choose one face
and explicitly map every material to a machine source slot.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import math

from .film_design import physical_thickness_nm, validate_design
from .film_design_service import simulation_settings
from .optical_constants import evaluate_optical_set, validate_optical_set
from .process_profiles import (MAX_STEP_DURATION_S, MAX_TARGET_NM,
                               source_selection_options)


FROZEN_KIND = "coat_sim.frozen_film_design"
FROZEN_SCHEMA_VERSION = 1
SURFACES = ("front", "back")


def _sha256(payload: Mapping) -> str:
    body = {key: value for key, value in payload.items() if key != "sha256"}
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _all_design_references(design: Mapping) -> list[dict]:
    references = [design["substrate"], *design["front_layers"], *design["back_layers"]]
    references.extend(design[key] for key in ("incident_medium", "exit_medium")
                      if design[key]["kind"] == "material")
    return [reference for reference in references
            if reference.get("material_id") and reference.get("optical_set_id")]


def freeze_complete_design(service, design: Mapping) -> dict:
    """Freeze a live design with its complete editable material closure.

    ``FilmDesignService.freeze_design`` is the authoritative snapshot producer,
    but it intentionally freezes only optically active references. Process
    import retains the complete two-face document, expression symbols and
    typed optimization candidates. Their bound material/data-set records are
    added from the same material provider before the digest is recomputed.
    Older numerical snapshots remain valid under the existing validator;
    editable restoration separately requires this larger dependency closure.
    """
    # Import lazily: project dependency parsing also validates process profiles,
    # whose frozen-provenance validation calls back into this module.
    from .project_dependencies import document_material_references

    normalized = validate_design(service.validate(design))
    required: dict[str, set[str]] = {}
    for material_id, set_id in document_material_references("film", normalized):
        required.setdefault(material_id, set()).add(set_id)
    frozen = service.freeze_design(normalized, {
        "first_surface": normalized["first_surface"],
    })
    if not isinstance(frozen, Mapping):
        raise ValueError("膜系服务未返回冻结快照")

    existing = {row["id"]: deepcopy(row) for row in frozen.get("materials", [])
                if isinstance(row, Mapping) and isinstance(row.get("id"), str)}

    records = []
    for material_id, set_ids in sorted(required.items()):
        base = existing.get(material_id)
        # The numerical snapshot can contain only the active datasets. Retain
        # the full provider record in its original dataset order so editable
        # same-project replay does not create an artificial content conflict.
        # Captured used values remain authoritative if the service supplied them.
        catalog_record = service.materials.get(material_id)
        if base is None:
            base = catalog_record
        sets = base.get("optical_sets") if isinstance(base, Mapping) else None
        catalog_sets = catalog_record.get("optical_sets")
        if not isinstance(sets, list) or not isinstance(catalog_sets, list):
            raise ValueError(f"材料 {material_id} 缺少光学数据集列表")
        selected = {}
        for collection in (catalog_sets, sets):
            seen = set()
            for optical_set in collection:
                validate_optical_set(optical_set)
                set_id = optical_set["id"]
                if set_id in seen:
                    raise ValueError(f"材料 {material_id} 的数据集 ID 重复：{set_id}")
                seen.add(set_id)
                selected[set_id] = deepcopy(optical_set)
        if not set_ids.issubset(selected):
            raise ValueError(f"材料 {material_id} 缺少设计引用的数据集："
                             + ", ".join(sorted(set_ids - set(selected))))
        # The numerical producer inserts design_ready=None even when the
        # provider omitted this optional metadata. Preserve the provider's
        # omission rather than creating a same-project exact-content conflict;
        # an explicitly captured value (including explicit None) stays intact.
        ready_metadata = ({"design_ready": base["design_ready"]}
                          if "design_ready" in base and
                          (base["design_ready"] is not None or
                           "design_ready" in catalog_record) else {})
        records.append({
            "id": material_id,
            "name": str(base.get("name", material_id)),
            "optical_sets": list(selected.values()),
            **ready_metadata,
            "notes": str(base.get("notes", "")),
        })

    payload = {key: deepcopy(value) for key, value in frozen.items() if key != "sha256"}
    payload["design"] = normalized
    payload["materials"] = records
    payload["sha256"] = _sha256(payload)
    validate_frozen_design_snapshot(payload)
    return payload


def validate_frozen_design_snapshot(snapshot: Mapping) -> dict:
    """Validate numerical/process references without rejecting older snapshots.

    Expression-symbol and optimization-candidate records were not captured by
    older producers. They are required for editable draft restoration, not for
    consuming the already expanded design as numerical/process provenance.
    """
    required = {"schema_version", "kind", "design", "settings", "materials", "sha256"}
    if not isinstance(snapshot, Mapping) or set(snapshot) != required:
        raise ValueError("冻结膜系快照字段无效")
    if type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != FROZEN_SCHEMA_VERSION:
        raise ValueError("冻结膜系快照版本无效")
    if snapshot.get("kind") != FROZEN_KIND:
        raise ValueError("冻结膜系快照类型无效")
    digest = snapshot.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("冻结膜系快照摘要格式无效")
    try:
        actual_digest = _sha256(snapshot)
    except (TypeError, ValueError) as exc:
        raise ValueError("冻结膜系快照包含不可序列化值") from exc
    if actual_digest != digest:
        raise ValueError("冻结膜系快照摘要不匹配")

    design = validate_design(snapshot["design"])
    if design != snapshot["design"]:
        raise ValueError("冻结膜系设计不是规范化格式")
    settings = simulation_settings(snapshot["settings"])
    if settings != snapshot["settings"]:
        raise ValueError("冻结膜系计算设置不是规范化格式")
    rows = snapshot.get("materials")
    if not isinstance(rows, list):
        raise ValueError("冻结膜系材料快照必须是数组")
    materials = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str) or not row["id"]:
            raise ValueError("冻结膜系材料记录无效")
        if row["id"] in materials:
            raise ValueError("冻结膜系材料 ID 重复")
        sets = row.get("optical_sets")
        if not isinstance(sets, list):
            raise ValueError(f"冻结材料 {row['id']} 的数据集列表无效")
        set_map = {}
        for optical_set in sets:
            if not isinstance(optical_set, Mapping) or not isinstance(optical_set.get("id"), str):
                raise ValueError(f"冻结材料 {row['id']} 的数据集记录无效")
            set_id = optical_set["id"]
            if set_id in set_map:
                raise ValueError(f"冻结材料 {row['id']} 的数据集 ID 重复")
            validate_optical_set(optical_set)
            set_map[set_id] = optical_set
        materials[row["id"]] = set_map

    for reference in _all_design_references(design):
        material_id = reference["material_id"]
        set_id = reference["optical_set_id"]
        if material_id not in materials or set_id not in materials[material_id]:
            raise ValueError(f"冻结膜系缺少材料/数据集 {material_id}/{set_id}")
    return deepcopy(dict(snapshot))


def _material_record(snapshot: Mapping, material_id: str, set_id: str) -> tuple[dict, dict]:
    material = next((row for row in snapshot["materials"] if row["id"] == material_id), None)
    if material is None:
        raise ValueError(f"冻结快照缺少材料 {material_id}")
    optical_set = next((row for row in material["optical_sets"] if row["id"] == set_id), None)
    if optical_set is None:
        raise ValueError(f"冻结快照缺少数据集 {material_id}/{set_id}")
    return material, optical_set


def physical_layer_thickness_nm(snapshot: Mapping, layer: Mapping) -> tuple[float, float | None]:
    """Return (physical nm, n at lambda0); QW conversion uses frozen n data."""
    design = snapshot["design"]
    reference_wavelength_nm = float(design["reference_wavelength_nm"])
    thickness = layer.get("thickness", {})
    n_reference = None
    if thickness.get("unit") == "qw":
        _material, optical_set = _material_record(
            snapshot, layer.get("material_id", ""), layer.get("optical_set_id", ""))
        n_reference, _k = evaluate_optical_set(
            optical_set, reference_wavelength_nm, "nm", extrapolation_policy="error")
        if not math.isfinite(float(n_reference)) or float(n_reference) <= 0:
            raise ValueError(f"层 {layer.get('id', '')} 的参考折射率无效")
    converted = physical_thickness_nm(layer, reference_wavelength_nm,
                                      1.0 if n_reference is None else n_reference)
    if not math.isfinite(converted) or not 0 < converted <= MAX_TARGET_NM:
        raise ValueError(f"层 {layer.get('id', '')} 的物理厚度超出工艺支持范围")
    return float(converted), (None if n_reference is None else float(n_reference))


def preview_film_design_import(snapshot: Mapping, surface: str) -> dict:
    """Create read-only import rows for one explicitly chosen design face."""
    frozen = validate_frozen_design_snapshot(snapshot)
    if surface not in SURFACES:
        raise ValueError("导入面必须明确选择 front 或 back")
    design = frozen["design"]
    if surface == "back" and design["substrate"]["sides"] != "double":
        raise ValueError("该膜系基片设置为单面，不能导入 back 面")
    layers = design[f"{surface}_layers"]
    if not layers:
        raise ValueError(f"所选 {surface} 面没有膜层")
    rows = []
    for index, layer in enumerate(layers, 1):
        thickness_nm, n_reference = physical_layer_thickness_nm(frozen, layer)
        material, optical_set = _material_record(
            frozen, layer.get("material_id", ""), layer.get("optical_set_id", ""))
        raw = layer["thickness"]
        rows.append({
            "index": index, "surface": surface, "layer_id": layer["id"],
            "material_id": layer["material_id"], "material_name": material["name"],
            "optical_set_id": layer["optical_set_id"],
            "optical_set_name": optical_set.get("name", layer["optical_set_id"]),
            "thickness_value": raw["value"], "thickness_unit": raw["unit"],
            "reference_wavelength_nm": design["reference_wavelength_nm"],
            "n_reference": n_reference, "physical_thickness_nm": thickness_nm,
        })
    other_surface = "back" if surface == "front" else "front"
    return {
        "design_id": design["id"], "design_name": design["name"],
        "snapshot_sha256": frozen["sha256"], "surface": surface,
        "sequence_order": "substrate_outward", "substrate_sides": design["substrate"]["sides"],
        "other_surface": other_surface,
        "other_surface_layer_count": len(design[f"{other_surface}_layers"]),
        "rows": rows,
    }


def build_film_process_import(snapshot: Mapping, surface: str,
                              source_slots_by_material_id: Mapping,
                              machine: Mapping) -> dict:
    """Build explicit process steps and import provenance without guessing slots."""
    frozen = validate_frozen_design_snapshot(snapshot)
    preview = preview_film_design_import(frozen, surface)
    if not isinstance(source_slots_by_material_id, Mapping):
        raise ValueError("材料到蒸发源的映射必须是对象")
    material_ids = list(dict.fromkeys(row["material_id"] for row in preview["rows"]))
    if set(source_slots_by_material_id) != set(material_ids):
        missing = [item for item in material_ids if item not in source_slots_by_material_id]
        extra = sorted(set(source_slots_by_material_id) - set(material_ids))
        details = []
        if missing:
            details.append("未映射材料 " + ", ".join(missing))
        if extra:
            details.append("多余映射 " + ", ".join(map(str, extra)))
        raise ValueError("；".join(details) or "材料源位映射不完整")

    options = {item["slot"]: item for item in source_selection_options(machine)}
    normalized_map = {}
    for material_id in material_ids:
        slot = source_slots_by_material_id[material_id]
        if type(slot) is not int or slot < 1:
            raise ValueError(f"材料 {material_id} 必须显式选择有效蒸发源位")
        option = options.get(slot)
        if option is None or not option.get("valid"):
            raise ValueError(f"材料 {material_id} 所选源位 {slot} 无效")
        normalized_map[material_id] = slot

    steps = []
    for row in preview["rows"]:
        slot = normalized_map[row["material_id"]]
        source = options[slot]
        rate = float(source["default_rate_nm_s"])
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f"第 {slot} 位源的默认沉积速率无效")
        duration = row["physical_thickness_nm"] / rate
        if not math.isfinite(duration) or not 0 < duration <= MAX_STEP_DURATION_S:
            raise ValueError(f"层 {row['layer_id']} 按所选源参考速率计算的名义时长超过工艺限制")
        steps.append({
            "source_slot": slot,
            "target_nm": row["physical_thickness_nm"],
            "rate_nm_s": rate,
            "target_mode": "mean",
            "film_layer_ref": {"surface": surface, "layer_id": row["layer_id"]},
            "film_design_thickness_nm": row["physical_thickness_nm"],
        })
    other_surface = preview["other_surface"]
    metadata = {
        "snapshot_sha256": frozen["sha256"],
        "surface": surface,
        "sequence_order": "substrate_outward",
        "layer_ids": [row["layer_id"] for row in preview["rows"]],
        "unexecuted_surfaces": [other_surface],
        "source_slots_by_material_id": normalized_map,
    }
    return {"steps": steps, "film_design_snapshot": frozen,
            "film_design_import": metadata, "preview": preview}
