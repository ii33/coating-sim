"""Editable material-library records for empirical source-distribution models.

These records contain user assumptions/calibrations, not a thermodynamic
property database. No vapor species, density or temperature-to-rate constants
are inferred from a material name. Source JSON embeds a full immutable copy.
"""
from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import uuid

from .atomic_io import replace_with_retry

SCHEMA_VERSION = 1
PROVENANCE_STATUSES = ("unvalidated", "theoretical", "calibrated")
_COMMON_DEFAULTS = {"source_exponent": 1.0, "reference_rate_nm_s": 1.0,
                    "rate_exponent_per_decade": 0.0,
                    "valid_rate_min_nm_s": None, "valid_rate_max_nm_s": None}
RESISTANCE_DEFAULTS = dict(_COMMON_DEFAULTS, active_fraction=1.0,
                           rate_active_fraction_per_decade=0.0, center_weight=0.0)
ELECTRON_BEAM_DEFAULTS = dict(_COMMON_DEFAULTS, spot_sigma_mm=3.0,
                              rate_spot_sigma_per_decade=0.0)
_LIBRARY_LOCKS = threading.local()
_LIBRARY_DESCRIPTORS = set()
_LIBRARY_DESCRIPTOR_LOCK = threading.Lock()


def _close_inherited_material_locks():
    """Drop child copies without unlocking the parent's shared flock."""
    global _LIBRARY_LOCKS
    try:
        for descriptor in _LIBRARY_DESCRIPTORS:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _LIBRARY_DESCRIPTORS.clear()
        _LIBRARY_LOCKS = threading.local()
    finally:
        _LIBRARY_DESCRIPTOR_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(before=_LIBRARY_DESCRIPTOR_LOCK.acquire,
                        after_in_parent=_LIBRARY_DESCRIPTOR_LOCK.release,
                        after_in_child=_close_inherited_material_locks)


@contextmanager
def material_library_lock(directory):
    """A reentrant, cross-process lock shared by GUI and business mutations.

    Keep the lock file after release: deleting it could let another caller lock
    a new inode while a waiter still holds the original file. This serializes
    writes and ID checks, but deliberately does not implement stale-read/CAS.
    """
    folder = Path(directory)
    if folder.is_symlink():
        raise ValueError("材料库不接受符号链接")
    folder.mkdir(parents=True, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("材料库必须是普通文件夹")
    key = os.path.normcase(str(folder.resolve()))
    held = getattr(_LIBRARY_LOCKS, "held", None)
    if held is None or getattr(_LIBRARY_LOCKS, "pid", None) != os.getpid():
        # A forked child must acquire its own OS lock, not mistake the
        # parent's inherited thread-local marker for a reentrant acquisition.
        _LIBRARY_LOCKS.pid = os.getpid()
        _LIBRARY_LOCKS.held = held = set()
    if key in held:
        yield
        return
    path = folder / ".material-library.lock"
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("材料库锁必须是普通文件")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    owner_pid = os.getpid()
    with _LIBRARY_DESCRIPTOR_LOCK:
        descriptor = os.open(path, flags, 0o600)
        _LIBRARY_DESCRIPTORS.add(descriptor)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("材料库锁必须是普通文件")
        if os.name == "nt":
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        held.add(key)
        try:
            yield
        finally:
            if os.getpid() == owner_pid:
                held.remove(key)
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        # The fork hook already closed child copies. Their descriptor numbers
        # may have been reused, so an inherited context must never close again.
        if os.getpid() == owner_pid:
            with _LIBRARY_DESCRIPTOR_LOCK:
                _LIBRARY_DESCRIPTORS.discard(descriptor)
                os.close(descriptor)


def finite_number(value, label):
    try:
        if isinstance(value, bool):
            raise TypeError()
        value = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label} 必须是有限数值") from exc
    if not math.isfinite(value):
        raise ValueError(f"{label} 必须是有限数值")
    return value


def _text(value, name, maximum=20000):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{name} 须为不超过 {maximum} 字的文字")
    return value


def validate_provenance(value=None):
    value = {} if value is None else value
    if not isinstance(value, Mapping) or set(value) - {"status", "reference", "notes"}:
        raise ValueError("provenance 须包含 status、reference 和 notes")
    status = value.get("status", "unvalidated")
    if status not in PROVENANCE_STATUSES:
        raise ValueError("来源状态须为 unvalidated、theoretical 或 calibrated")
    reference = _text(value.get("reference", ""), "来源参考")
    notes = _text(value.get("notes", ""), "来源备注")
    if status == "calibrated" and not reference.strip():
        raise ValueError("已校准分布必须填写实测或标定来源")
    return {"status": status, "reference": reference, "notes": notes}


def validate_model_defaults(value, kind):
    template = RESISTANCE_DEFAULTS if kind == "resistance_boat" else ELECTRON_BEAM_DEFAULTS
    if not isinstance(value, Mapping) or set(value) - set(template):
        raise ValueError("材料模型 defaults 包含未知参数")
    result = dict(template)
    result.update(value)
    for key in result:
        if key.startswith("valid_rate_") and result[key] is None:
            continue
        result[key] = finite_number(result[key], key)
    if not 0 <= result["source_exponent"] <= 20:
        raise ValueError("源角指数必须在 0–20 之间")
    if not 0 < result["reference_rate_nm_s"] <= 1e9:
        raise ValueError("参考速率必须为正且不超过 1e9 nm/s")
    for key in ("valid_rate_min_nm_s", "valid_rate_max_nm_s"):
        if result[key] is not None and not 0 < result[key] <= 1e9:
            raise ValueError("有效速率范围必须为正且不超过 1e9 nm/s")
    low, high = result["valid_rate_min_nm_s"], result["valid_rate_max_nm_s"]
    if low is not None and high is not None and low > high:
        raise ValueError("有效速率下限不得大于上限")
    if ((low is not None and result["reference_rate_nm_s"] < low) or
            (high is not None and result["reference_rate_nm_s"] > high)):
        raise ValueError("参考速率必须位于有效速率范围内")
    for key in result:
        if key.startswith("rate_") and abs(result[key]) > 100:
            raise ValueError("速率响应系数绝对值不得超过 100")
    if kind == "resistance_boat":
        if not 0 < result["active_fraction"] <= 1:
            raise ValueError("有效尺寸比例必须在 0–1 之间")
        if not 0 <= result["center_weight"] <= 1:
            raise ValueError("中心加权比例必须在 0–1 之间")
    elif not 0 < result["spot_sigma_mm"] <= 10000:
        raise ValueError("等效发射高斯 σ 必须在 0–10000 mm 之间")
    return result


def validate_material_profile(data):
    if not isinstance(data, Mapping):
        raise ValueError("材料记录必须是 JSON 对象")
    allowed = {"schema_version", "id", "name", "description", "notes",
               "resistance_model", "electron_beam_model", "reference_data", "research_data", "roles", "optical_constants", "category", "optical_only"}
    if set(data) - allowed:
        raise ValueError("材料记录包含未知参数：" + "、".join(sorted(set(data) - allowed)))
    if (isinstance(data.get("schema_version", 1), bool) or
            data.get("schema_version", 1) != SCHEMA_VERSION):
        raise ValueError("材料 JSON 版本必须是 1")
    identifier = data.get("id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", identifier):
        raise ValueError("材料 id 须为 1–80 个字母、数字、下划线或连字符")
    if identifier.upper() in {"CON", "PRN", "AUX", "NUL"} | {f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)}:
        raise ValueError("材料 id 不能使用系统设备名")
    name = _text(data.get("name"), "材料名称", 120).strip()
    if not name:
        raise ValueError("材料名称不能为空")
    result = {"schema_version": SCHEMA_VERSION, "id": identifier, "name": name,
              "description": _text(data.get("description", ""), "材料描述"),
              "notes": _text(data.get("notes", ""), "材料备注")}
    optical_only = data.get("optical_only", False)
    if type(optical_only) is not bool:
        raise ValueError("optical_only 必须是明确的 bool")
    if "optical_only" in data:
        result["optical_only"] = optical_only
    for field, kind in (("resistance_model", "resistance_boat"),
                        ("electron_beam_model", "electron_beam")):
        if optical_only:
            if data.get(field) is not None:
                raise ValueError("仅光学资料材料的工艺源模型须保持 null；补齐模型前请显式退出 optical_only")
            result[field] = None
            continue
        model = data.get(field, {})
        if not isinstance(model, Mapping) or set(model) - {"defaults", "provenance"}:
            raise ValueError(f"{field} 须包含 defaults 和 provenance")
        if "optical_only" in data:
            template = RESISTANCE_DEFAULTS if kind == "resistance_boat" else ELECTRON_BEAM_DEFAULTS
            defaults = model.get("defaults")
            if not isinstance(defaults, Mapping) or set(defaults) != set(template):
                raise ValueError("显式退出 optical_only 须完整提供并验证两套工艺源模型，不能补入未提供的默认参数")
        result[field] = {"defaults": validate_model_defaults(model.get("defaults", {}), kind),
                         "provenance": validate_provenance(model.get("provenance"))}
    if data.get("reference_data") is not None:
        from .material_physics import validate_reference_data
        result["reference_data"] = validate_reference_data(data["reference_data"])
    if data.get("roles") is not None:
        from .material_research import ROLES
        roles = data["roles"]
        if (not isinstance(roles, list) or not roles or
                any(not isinstance(role, str) or role not in ROLES for role in roles) or
                len(set(roles)) != len(roles)):
            raise ValueError("材料用途须为非空、不重复的支持用途列表")
        result["roles"] = list(roles)
    if data.get("research_data") is not None:
        from .material_research import validate_research_data
        result["research_data"] = validate_research_data(data["research_data"])
        actual_roles = result["research_data"]["roles"]
        if "roles" in result and result["roles"] != actual_roles:
            raise ValueError("材料用途不得与研究来源快照不一致")
        result["roles"] = list(actual_roles)
        if "reference_data" in result and (
                result["research_data"]["id"].casefold() not in ("zns", "zns_film") or
                "film" not in actual_roles):
            raise ValueError("ZnS计算参考与研究材料身份或用途不一致；不能跨材料使用物理模型")
    if data.get("optical_constants") is not None:
        from .optical_constants import validate_optical_sets
        result["optical_constants"] = validate_optical_sets(data["optical_constants"])
    if data.get("category") is not None:
        category = data["category"]
        if category not in ("film", "substrate", "other"):
            raise ValueError("材料分类须为 film、substrate 或 other")
        if "roles" in result and category != "other" and category not in material_categories(result):
            raise ValueError("材料分类与原用途角色不一致")
        result["category"] = category
    return result


def material_categories(material):
    """Return only explicitly supported film/substrate memberships.

    Names, medium usage, component/boat/element roles and missing metadata never
    imply film or substrate membership. Multiple explicit roles remain visible
    in each pool. Unclassified source records stay unchanged pending a decision.
    """
    roles = material.get("roles")
    if roles is not None:
        return tuple(role for role in ("film", "substrate") if role in roles)
    category = material.get("category")
    return (category,) if category in {"film", "substrate"} else ()


def material_category(material):
    """Primary category for existing callers; None never means film."""
    categories = material_categories(material)
    return categories[0] if categories else None


def check_material_name(record, records):
    """Reject another identity's name only within this record's category."""
    key = record["name"].strip().casefold()
    for other in records:
        if (other["id"].casefold() != record["id"].casefold()
                and set(material_categories(other)).intersection(material_categories(record))
                and other["name"].strip().casefold() == key):
            raise ValueError(f"同一材料分类已存在同名材料“{other['name']}”；请使用不同名称")


def material_display_name(material):
    name = material["name"]
    for suffix in (" · 文献参考", " · 研究参考"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return name


def source_bindable(material):
    """Legacy user assumptions remain usable; researched non-films are browse-only."""
    roles = material.get("roles")
    return (material.get("optical_only", False) is not True
            and ("film" in roles if roles is not None else material.get("category", "film") == "film"))


def substrate_bindable(material):
    roles = material.get("roles")
    return bool({"substrate", "medium"}.intersection(roles)) if roles is not None else material.get("category") == "substrate"


def default_material_profile(name="新建材料", id=None, *, optical_only=False):
    if type(optical_only) is not bool:
        raise ValueError("optical_only 必须是明确的 bool")
    record = {"id": id or uuid.uuid4().hex, "name": name}
    if optical_only:
        record["optical_only"] = True
    return validate_material_profile(record)


def _optical_catalog_by_builtin_id(profiles):
    """Read sourced optical groups without changing source-model defaults."""
    from .data_files import data_path
    from .optical_constants import validate_optical_sets

    path = data_path("optical_catalog.json")
    if not path.is_file():
        return {}
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Built-in optical catalog is unreadable: {exc}") from exc
    if not isinstance(catalog, Mapping) or catalog.get("schema_version") != 1:
        raise ValueError("Built-in optical catalog must use schema version 1")
    rows = catalog.get("materials")
    if not isinstance(rows, list):
        raise ValueError("Built-in optical catalog needs a materials list")

    by_id = {record["id"]: record for record in profiles}
    result = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Built-in optical catalog contains an invalid material")
        identity = row.get("builtin_material_id")
        profile = by_id.get(identity)
        if profile is None or identity in result:
            raise ValueError(f"Unknown or duplicate built-in optical material: {identity!r}")
        research_id = (profile.get("research_data") or {}).get("id", profile["id"])
        if row.get("material_id") != research_id:
            raise ValueError(f"Optical catalog material ID does not match {identity}")
        if row.get("category") != material_category(profile):
            raise ValueError(f"Optical catalog role does not match {identity}")
        groups = validate_optical_sets(row.get("optical_constants"))
        if not groups:
            raise ValueError(f"Optical catalog has no groups for {identity}")
        if any(not group["notes"].strip() or not group["source"].strip() for group in groups):
            raise ValueError(f"Optical catalog source notes are incomplete for {identity}")
        result[identity] = groups
    return result


def builtin_material_profiles():
    """Read-only reference presets; selecting/copying never writes user files."""
    from .material_physics import reference_material_data
    record = default_material_profile("ZnS", id="builtin_zns_literature_20261004")
    record["description"] = "T04/T05 已有 ZnS 证据与有界文献参考计算；非机台标定材料"
    record["notes"] = "QCM设置密度不等于实测膜密度；厂家蒸气压定义不明，不能直接作为Zn/S₂分压。"
    record["reference_data"] = reference_material_data("ZnS")
    for field in ("resistance_model", "electron_beam_model"):
        record[field]["provenance"] = {"status": "theoretical",
            "reference": "局部余弦/有效足迹为空间模型假设；并非ZnS实测角分布",
            "notes": "文献热化学参考见 reference_data；未提供该设备的已标定空间参数"}
    from .material_research import research_material_records
    records = [record]
    for research in research_material_records():
        if research["id"].casefold() in ("zns", "zns_film"):
            record["research_data"] = research
            record["roles"] = research["roles"]
            continue
        # Keep the complete historical title in research_data; use a concise
        # material label in selectors, with its role displayed separately.
        display_name = research["name"].split(" ", 1)[0]
        display_name = display_name.replace("（源容器）", "").replace("基片（方法示范）", "")
        item = default_material_profile(display_name, id="builtin_research_" + research["id"])
        item["description"] = research.get("description", "")
        item["notes"] = "来源物性保留各自条件；空间分布参数为项目约定，未作材料专属标定。"
        item["roles"] = research["roles"]
        item["research_data"] = research
        records.append(item)
    air = default_material_profile("Air", id="builtin_ideal_air", optical_only=True)
    air["description"] = "理想空气光学占位模型（n=1，k=0）；本项目近似，不提供实体基片或蒸发源物性"
    air["notes"] = "理想非色散、无吸收近似；不描述实际空气的色散或吸收。"
    air["roles"] = ["film", "medium"]
    records.append(air)
    optical_catalog = _optical_catalog_by_builtin_id(records)
    for record in records:
        additions = optical_catalog.get(record["id"], [])
        if not additions:
            continue
        existing = list(record.get("optical_constants") or [])
        by_set_id = {row["id"]: row for row in existing}
        for row in additions:
            previous = by_set_id.get(row["id"])
            if previous is not None:
                if previous != row:
                    raise ValueError(f"Optical group conflicts with an existing built-in group: {row['id']}")
                continue
            existing.append(deepcopy(row))
            by_set_id[row["id"]] = row
        record["optical_constants"] = existing
    # Independent ordinary records: shared numerical values, distinct ownership.
    substrate_air = deepcopy(air)
    substrate_air["id"] = "builtin_ideal_air_substrate"
    substrate_air["roles"] = ["substrate"]
    records.append(substrate_air)
    # These records already existed in the film picker. Share those exact
    # definitions with the unified catalog rather than inventing replacements.
    from .optical_substrate_presets import builtin_optical_substrates
    for preset in builtin_optical_substrates():
        profile = default_material_profile(preset["name"], id=preset["id"], optical_only=True)
        groups = deepcopy(preset["optical_sets"])
        # New selections use the requested nm representation. Existing µm set
        # IDs/data remain available unchanged for saved project references.
        groups.sort(key=lambda group: group["wavelength_unit"] != "nm")
        profile.update(category="substrate", roles=list(preset["roles"]),
            notes=preset["notes"], optical_constants=groups)
        records.append(profile)
    return [validate_material_profile(item) for item in records]


def material_emission_model(material, kind):
    """Copy a heater's defaults; callers keep source_exponent at source level."""
    material = validate_material_profile(material)
    if material.get("optical_only", False):
        raise ValueError("仅光学资料材料的工艺源模型未知，不能绑定或计算蒸发源")
    if not source_bindable(material):
        raise ValueError("此记录是基片、组分、元素或舟材料，不能作为蒸发薄膜材料绑定到源")
    if kind not in ("resistance_boat", "electron_beam"):
        raise ValueError("材料模型须选择 resistance_boat 或 electron_beam")
    field = "resistance_model" if kind == "resistance_boat" else "electron_beam_model"
    model = material[field]
    result = deepcopy(model["defaults"])
    result.pop("source_exponent")
    result.update(kind=kind, provenance=deepcopy(model["provenance"]))
    return result


def material_completeness(profile):
    material = validate_material_profile(profile)
    models = {}
    for key in ("resistance_model", "electron_beam_model"):
        model = material[key]
        if model is None:
            models[key] = {"status": "unknown", "distribution_ready": False,
                           "calibration_declared": False, "missing": ["工艺源模型未提供"]}
            continue
        calibrated = model["provenance"]["status"] == "calibrated"
        models[key] = {"status": model["provenance"]["status"],
                       "distribution_ready": True,
                       "calibration_declared": calibrated,
                       "missing": [] if calibrated else ["实测空间分布标定"]}
    from .material_research import property_coverage
    bindable = source_bindable(material)
    for model in models.values():
        model["distribution_ready"] = bindable
    return {"models": models, "distribution_ready": bindable,
            "roles": material.get("roles", []), "source_bindable": bindable,
            "research_available": "research_data" in material,
            "property_coverage": property_coverage(material.get("research_data")),
            "thermodynamic_rate_available": False,
            "thermochemical_reference_available": "reference_data" in material,
            "reference_models": list(material.get("reference_data", {}).get("models", {})),
            "warning": ("仅光学资料；工艺源模型未知，补齐并验证前不能绑定蒸发源" if material.get("optical_only") is True else
                        "已接有界文献参考压力/条件源通量；不是机台沉积率或热场预测" if "reference_data" in material else
                        "参数为用户假设或用户声明的标定；尚无材料专属热化学参考数据")}


def read_material_profile(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取材料文件：{exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("材料文件必须是第 1 版 JSON")
    return validate_material_profile(data)


def list_materials(directory):
    folder = Path(directory)
    if not folder.exists():
        return []
    if not folder.is_dir() or folder.is_symlink():
        raise ValueError("材料库必须是普通文件夹")
    records = []
    seen = set()
    for path in sorted(folder.glob("*.json")):
        if path.is_symlink():
            raise ValueError("材料库不接受符号链接")
        record = read_material_profile(path)
        if record["id"] in seen:
            raise ValueError("材料库存在重复 id：" + record["id"])
        seen.add(record["id"])
        records.append(record)
    return sorted(records, key=lambda item: (item["name"].casefold(), item["id"]))


def save_material(directory, profile, overwrite=False):
    record = validate_material_profile(profile)
    folder = Path(directory)
    with material_library_lock(folder):
        return _save_material_locked(folder, record, overwrite)


def _save_material_locked(folder, record, overwrite):
    path = folder / (record["id"] + ".json")
    matching = []
    for candidate in folder.glob("*.json"):
        info = candidate.lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("材料文件必须是普通文件")
        existing = read_material_profile(candidate)
        check_material_name(record, [existing])
        if existing["id"].casefold() == record["id"].casefold():
            matching.append((candidate, existing["id"]))
    if len(matching) > 1:
        raise ValueError("材料库存在忽略大小写的重复 id：" + record["id"])
    if matching:
        if not overwrite:
            raise ValueError("同一 id 的材料已存在；请明确覆盖或使用新 id")
        if matching[0][1] != record["id"]:
            raise ValueError("覆盖材料须使用已有记录的准确 id")
        # A valid record's filename need not match its ID. Replace the file
        # that owns this ID instead of publishing a duplicate canonical file.
        path = matching[0][0]
        if (read_material_profile(path).get("optical_only") is True and
                record.get("optical_only") is not True and record.get("optical_only") is not False):
            raise ValueError("退出已有 optical_only 材料须显式设为 false 并完整提供两套工艺源模型")
    if path.is_symlink():
        raise ValueError("材料文件不接受符号链接")
    payload = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            replace_with_retry(temporary, path)
        else:
            try:
                # Same-directory hard-link publication is atomic and refuses
                # an existing destination, even if another writer raced us.
                os.link(temporary, path)
            except FileExistsError as exc:
                raise ValueError("同一 id 的材料已存在；请明确覆盖或使用新 id") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return deepcopy(record)


def delete_material(directory, identifier):
    """Delete a library entry only; already embedded source copies are unaffected."""
    validate_material_profile({"id": identifier, "name": "检查"})
    with material_library_lock(directory):
        path = Path(directory) / (identifier + ".json")
        if path.is_symlink():
            raise ValueError("材料库不接受符号链接")
        path.unlink()
