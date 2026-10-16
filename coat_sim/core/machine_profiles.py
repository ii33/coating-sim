"""Machine geometry, dependency paths, and runtime dependency resolution.

The v5 machine file stores placements and relative source/board references.
The process selects a substrate separately from the machine.
"""

from .data_files import NAMED_METADATA_FIELDS, design_metadata
from .design_files import validate_design_path

import json
import math
from pathlib import Path

from .gear_geometry import center_distance_from_module, module_from_center_distance
from .source_profiles import read_source_profile

SCHEMA_VERSION = 5
MAX_SOURCES = 64
MAX_MONITORS = 64
MONITOR_KINDS = ("crystal", "optical")
MACHINE_FIELDS = ("carrier_x_mm", "carrier_y_mm", "planet_offset_mm",
                  "substrate_z_mm", "sun_teeth", "planet_teeth")
MACHINE_LABELS = {
    "carrier_x_mm": "载架中心 X / mm",
    "carrier_y_mm": "载架中心 Y / mm",
    "planet_offset_mm": "行星盘轨道半径 / mm",
    "planet_tilt_deg": "行星盘径向倾角 / °",
    "substrate_z_mm": "基片中心高度 / mm",
    "sun_teeth": "太阳轮齿数",
    "planet_teeth": "行星轮齿数",
    "gear_module_mm": "齿轮模数 / mm",
}


def _number(value, label):
    try:
        if isinstance(value, bool):
            raise TypeError()
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须是有限数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}必须是有限数值")
    return number


def _relative_file(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}须选择文件")
    value = value.strip()
    path = Path(value)
    if path.drive and not path.is_absolute():
        raise ValueError(f"{label}不能使用带盘符的非绝对路径")
    return value


def _polygon(points, label):
    if not isinstance(points, list) or not 3 <= len(points) <= 500:
        raise ValueError(f"{label}须有 3–500 个顶点")
    result = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"{label}顶点须为 [X,Y]")
        result.append([_number(point[0], label + " X"),
                       _number(point[1], label + " Y")])
    twice_area = math.fsum(
        result[i - 1][0] * result[i][1] - result[i][0] * result[i - 1][1]
        for i in range(len(result)))
    if abs(twice_area) < 1e-9:
        raise ValueError(f"{label}面积为零")
    return result


def validate_mask_profile(mask, *, substrate_z_mm=None):
    """Validate a bare fixed-frame correction-board JSON object."""
    if not isinstance(mask, dict) or set(mask) - NAMED_METADATA_FIELDS not in (
            {"z_mm", "outer"}, {"z_mm", "outer", "holes"}):
        raise ValueError("修正板须包含 z_mm、outer 和可选 holes")
    z = _number(mask["z_mm"], "修正板高度")
    if substrate_z_mm is not None and z >= substrate_z_mm:
        raise ValueError("修正板高度须低于基片中心；与源及倾斜盘的射线关系在计算时检查")
    holes = mask.get("holes", [])
    if not isinstance(holes, list) or len(holes) > 30:
        raise ValueError("修正板孔洞最多 30 个")
    return {"z_mm": z, "outer": _polygon(mask["outer"], "外轮廓"),
            **design_metadata(mask, include_name=True), "holes": [_polygon(hole, "孔洞") for hole in holes]}


def read_mask_profile(path, *, substrate_z_mm=None):
    validate_design_path("mask", path)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取修正板文件：{exc}") from exc
    return validate_mask_profile(data, substrate_z_mm=substrate_z_mm)


def new_monitor(kind="crystal", name="晶控 1", *, x_mm=0.0,
                y_mm=0.0, z_mm=100.0):
    """Create an unbound monitor record at a fixed machine-frame XYZ."""
    return _monitor_entry({"name": name, "kind": kind,
                           "x_mm": x_mm, "y_mm": y_mm, "z_mm": z_mm})


def _monitor_entry(entry):
    if not isinstance(entry, dict) or set(entry) != {
            "name", "kind", "x_mm", "y_mm", "z_mm"}:
        raise ValueError("监测器须包含 name、kind、x_mm、y_mm、z_mm")
    name = entry["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("监测器名称须为 1–80 个字符")
    kind = entry["kind"]
    if kind not in MONITOR_KINDS:
        raise ValueError("监测器类型须为 crystal 或 optical")
    xyz = {key: _number(entry[key], f"监测器 {label}")
           for key, label in (("x_mm", "X"), ("y_mm", "Y"), ("z_mm", "Z"))}
    return {"name": name.strip(), "kind": kind, **xyz}


def _source_entry(entry):
    if not isinstance(entry, dict) or not {"file", "x_mm", "y_mm"} <= set(entry) or \
            set(entry) - {"file", "x_mm", "y_mm", "z_mm"}:
        raise ValueError("源安装项须包含 file、x_mm、y_mm，可选 z_mm")
    return {"file": _relative_file(entry["file"], "源文件"),
            "x_mm": _number(entry["x_mm"], "源中心 X"),
            "y_mm": _number(entry["y_mm"], "源中心 Y"),
            "z_mm": _number(entry.get("z_mm", 0.0), "源中心 Z")}


def _mask_entry(entry):
    if not isinstance(entry, dict) or set(entry) != {"file"}:
        raise ValueError("修正板项须只包含 file")
    return {"file": _relative_file(entry["file"], "修正板文件")}


def validate_machine_profile(profile):
    """Normalize v5 machine structure without touching dependency files."""
    if not isinstance(profile, dict):
        raise ValueError("机器 JSON 顶层必须是对象")
    expected = set(MACHINE_FIELDS) | {"schema_version", "gear_module_mm",
        "planet_tilt_deg", "sources", "masks", "monitors"}
    missing = expected - set(profile) - {"planet_tilt_deg"}
    extra = set(profile) - expected - NAMED_METADATA_FIELDS
    if missing:
        raise ValueError("缺少机器参数：" + "、".join(sorted(missing)))
    if extra:
        raise ValueError("不认识的机器参数：" + "、".join(sorted(extra)))
    if isinstance(profile["schema_version"], bool) or profile["schema_version"] != SCHEMA_VERSION:
        raise ValueError("机器 JSON 版本必须是 5")
    geometry = {}
    for key in MACHINE_FIELDS:
        value = _number(profile[key], MACHINE_LABELS[key])
        if key in ("sun_teeth", "planet_teeth"):
            if not 1 <= value <= 1000 or int(value) != value:
                raise ValueError(f"{MACHINE_LABELS[key]}须为 1–1000 的整数")
            value = int(value)
        geometry[key] = value
    if not 0 <= geometry["planet_offset_mm"] <= 10000:
        raise ValueError("行星盘轨道半径须在 0–10000 mm 之间")
    if not 0 < geometry["substrate_z_mm"] <= 100000:
        raise ValueError("基片中心高度须在 0–100000 mm 之间")
    tilt = _number(profile.get("planet_tilt_deg", 0.0), MACHINE_LABELS["planet_tilt_deg"])
    if not -80 <= tilt <= 80:
        raise ValueError("行星盘径向倾角须在 -80–80° 之间")
    geometry["planet_tilt_deg"] = tilt
    module = _number(profile["gear_module_mm"], "齿轮模数")
    if module < 0:
        raise ValueError("齿轮模数不能小于 0")
    distance = center_distance_from_module(
        module, geometry["sun_teeth"], geometry["planet_teeth"])
    if not math.isclose(distance, geometry["planet_offset_mm"],
                        rel_tol=1e-9, abs_tol=1e-7):
        raise ValueError("模数与轨道半径不一致：R = m × (太阳轮齿数 + 行星轮齿数) / 2")
    raw_sources = profile["sources"]
    if not isinstance(raw_sources, list) or not 1 <= len(raw_sources) <= MAX_SOURCES:
        raise ValueError(f"机器须包含 1–{MAX_SOURCES} 个蒸发源")
    raw_masks = profile["masks"]
    if not isinstance(raw_masks, list):
        raise ValueError("机器修正板须为列表")
    raw_monitors = profile["monitors"]
    if not isinstance(raw_monitors, list) or len(raw_monitors) > MAX_MONITORS:
        raise ValueError(f"一台机器最多有 {MAX_MONITORS} 个监测器")
    sources = [_source_entry(item) for item in raw_sources]
    masks = [_mask_entry(item) for item in raw_masks]
    monitors = [_monitor_entry(item) for item in raw_monitors]
    names = [item["name"].casefold() for item in monitors]
    if len(names) != len(set(names)):
        raise ValueError("同一机器中的监测器名称不能重复")
    return {"schema_version": SCHEMA_VERSION, **geometry,
            "gear_module_mm": module,
            "sources": sources, "masks": masks, "monitors": monitors, **design_metadata(profile, include_name=True)}


def _dependency_path(base_dir, relative_path):
    return (Path(base_dir).resolve() / relative_path).resolve()


def resolve_machine_profile(profile, base_dir):
    """Return (runtime bundle, dependency errors), preserving source/board slots.

    Each bundle source is {file,x_mm,y_mm,profile}; each board is {file,profile}.
    An invalid dependent profile is None, so numbered process slots stay stable.
    """
    normalized = validate_machine_profile(profile)
    bundle = dict(normalized)
    bundle["sources"] = []
    bundle["masks"] = []
    errors = []
    for index, entry in enumerate(normalized["sources"], 1):
        loaded = dict(entry, profile=None)
        try:
            loaded["profile"] = read_source_profile(
                _dependency_path(base_dir, entry["file"]))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append(f"sources[{index}].file: {exc}")
        bundle["sources"].append(loaded)
    for index, entry in enumerate(normalized["masks"], 1):
        loaded = dict(entry, profile=None)
        try:
            loaded["profile"] = read_mask_profile(
                _dependency_path(base_dir, entry["file"]),
                substrate_z_mm=normalized["substrate_z_mm"])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append(f"masks[{index}].file: {exc}")
        bundle["masks"].append(loaded)
    return bundle, errors


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"机器 JSON 中参数重复：{key}")
        result[key] = value
    return result


def read_machine_profile(path):
    validate_design_path("machine", path)
    if not path or not str(path).strip():
        raise ValueError("未选择机器文件")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"),
                          object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取机器文件：{exc}") from exc
    return validate_machine_profile(data)


def write_machine_profile(path, profile):
    validate_design_path("machine", path)
    normalized = validate_machine_profile(profile)
    _, errors = resolve_machine_profile(normalized, Path(path).parent)
    if errors:
        raise ValueError("依赖文件无效，无法保存：\n" + "\n".join(errors))
    Path(path).write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    return normalized


def new_machine_profile(settings=None):
    """Create an unsaved machine draft; choose its dependency files before save."""
    def setting(name, default):
        return getattr(settings, name, default) if settings is not None else default
    sun = int(setting("sun_teeth", 120))
    planet = int(setting("planet_teeth", 36))
    distance = float(setting("planet_offset_mm", 312.0))
    module = module_from_center_distance(distance, sun, planet)
    return validate_machine_profile({
        "schema_version": SCHEMA_VERSION,
        "carrier_x_mm": float(setting("carrier_x_mm", 0.0)),
        "carrier_y_mm": float(setting("carrier_y_mm", 0.0)),
        "planet_offset_mm": distance,
        "planet_tilt_deg": float(setting("planet_tilt_deg", 0.0)),
        "substrate_z_mm": float(setting("substrate_z_mm", 1000.0)),
        "sun_teeth": sun, "planet_teeth": planet,
        "gear_module_mm": module,
        "sources": [{"file": "source_example.json",
                     "x_mm": float(setting("source_x_mm", 100.0)),
                     "y_mm": float(setting("source_y_mm", 0.0)),
                     "z_mm": float(setting("source_z_mm", 0.0))}],
        "masks": [], "monitors": [],
    })
