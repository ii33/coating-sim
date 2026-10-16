"""Location-free evaporation-source designs and discrete source elements."""

from .data_files import NAMED_METADATA_FIELDS, design_metadata
from .materials import (validate_material_profile, material_emission_model,
                        validate_provenance, validate_model_defaults, source_bindable)

from collections.abc import Mapping
import json
import math
from pathlib import Path

SCHEMA_VERSION = 2
SOURCE_KINDS = ("point", "line", "disk", "annulus", "rectangle")
DEVICE_TYPES = ("custom", "resistance_boat", "crucible", "electron_beam")


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


def _size(value, label):
    number = _number(value, label)
    if not 0 < number <= 10000:
        raise ValueError(f"{label}必须在 0–10000 mm 之间")
    return number


def validate_source_profile(data):
    """Normalize a source design; position is defined by its machine placement."""
    if not isinstance(data, Mapping):
        raise ValueError("蒸发源配置必须是 JSON 对象")
    if "schema_version" in data and (isinstance(data["schema_version"], bool)
                                     or data["schema_version"] != SCHEMA_VERSION):
        raise ValueError("蒸发源 JSON 版本必须是 2")
    kind = data.get("kind")
    if kind not in SOURCE_KINDS:
        raise ValueError("蒸发源形状必须是 point、line、disk、annulus 或 rectangle")
    device = data.get("device_type", "custom")
    if device not in DEVICE_TYPES:
        raise ValueError("蒸发设备类型无效")
    allowed = {"schema_version", "kind", "device_type", "source_exponent",
               "material", "emission_model", "thermochemistry", "sampling_order"}
    allowed |= {
        "point": set(), "line": {"length_mm", "angle_deg"},
        "disk": {"radius_mm"}, "annulus": {"inner_radius_mm", "outer_radius_mm"},
        "rectangle": {"length_mm", "width_mm", "angle_deg"},
    }[kind]
    extra = set(data) - allowed - NAMED_METADATA_FIELDS
    if extra:
        raise ValueError("源文件包含不允许的参数：" + "、".join(sorted(extra))
                         + "；源坐标应在机器中填写")
    material = (validate_material_profile(data["material"])
                if data.get("material") is not None else None)
    if material is not None and not source_bindable(material):
        raise ValueError("此材料用途不支持绑定到蒸发源；基片、组分和舟材料仅供查阅")
    material_field = "resistance_model" if device == "resistance_boat" else "electron_beam_model"
    default_exponent = (material[material_field]["defaults"]["source_exponent"]
                        if material and device in ("resistance_boat", "electron_beam") else 1.0)
    exponent = _number(data.get("source_exponent", default_exponent), "源角指数")
    if not 0 <= exponent <= 20:
        raise ValueError("源角指数必须在 0–20 之间")
    result = {"schema_version": SCHEMA_VERSION, "kind": kind,
              "device_type": device, "source_exponent": exponent}
    if data.get("sampling_order") is not None:
        order = data["sampling_order"]
        if type(order) is not int or not 3 <= order <= 41 or order % 2 != 1:
            raise ValueError("源空间采样阶数须为 3–41 的奇数；留空保持原有采样")
        result["sampling_order"] = order
    if kind in ("line", "rectangle"):
        result["length_mm"] = _size(data.get("length_mm"),
                                     "矩形源长度" if kind == "rectangle" else "线源长度")
        if kind == "rectangle":
            result["width_mm"] = _size(data.get("width_mm"), "矩形源宽度")
        angle = _number(data.get("angle_deg", 0), "源方向")
        result["angle_deg"] = (angle + 180) % 360 - 180
    elif kind == "disk":
        result["radius_mm"] = _size(data.get("radius_mm"), "圆盘源半径")
    elif kind == "annulus":
        inner = _number(data.get("inner_radius_mm"), "环形源内半径")
        outer = _size(data.get("outer_radius_mm"), "环形源外半径")
        if not 0 <= inner < outer:
            raise ValueError("环形源半径必须满足 0 ≤ 内半径 < 外半径")
        result.update(inner_radius_mm=inner, outer_radius_mm=outer)
    if material is not None:
        result["material"] = material
    model = data.get("emission_model")
    if model is None and material and device in ("resistance_boat", "electron_beam"):
        model = material_emission_model(material, device)
    if model is not None:
        result["emission_model"] = validate_emission_model(model, material)
    if data.get("thermochemistry") is not None:
        from .material_physics import validate_thermochemistry
        if material is None or material.get("reference_data", {}).get("material_id") != "ZnS":
            raise ValueError("ZnS文献参考计算须绑定包含ZnS文献数据的材料快照")
        result["thermochemistry"] = validate_thermochemistry(data["thermochemistry"])
    result.update(design_metadata(dict(data), include_name=True))
    return result


def read_source_profile(path):
    """Read a standalone v2 source JSON."""
    if not path or not str(path).strip():
        raise ValueError("未选择蒸发源文件")
    from .design_files import validate_design_path
    path = validate_design_path("source", path)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取蒸发源文件：{exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("蒸发源文件必须是第 2 版 JSON")
    return validate_source_profile(data)


def write_source_profile(path, profile):
    from .design_files import validate_design_path
    path = validate_design_path("source", path)
    normalized = validate_source_profile(profile)
    Path(path).write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    return normalized


def _ring_points(radius, count, phase=0.0):
    import numpy as np

    angles = phase + 2 * math.pi * np.arange(count, dtype=float) / count
    return np.column_stack((radius * np.cos(angles), radius * np.sin(angles)))


def source_samples(settings, profile=None, *, rate_nm_s=None):
    """Return machine-placed XYZ quadrature nodes and normalized weights.

    A finite source is parallel to the fixed machine XY plane; every element
    has the placement height ``settings.source_z_mm``.
    """
    import numpy as np

    center = np.asarray((settings.source_x_mm, settings.source_y_mm,
                         settings.source_z_mm), dtype=float)
    if not np.isfinite(center).all():
        raise ValueError("蒸发源中心坐标必须是有限数值")
    profile = validate_source_profile({"kind": "point"} if profile is None else profile)
    model = profile.get("emission_model", {"kind": "uniform"})
    rate = (_model_rate(settings, model, rate_nm_s)
            if "emission_model" in profile else None)
    kind = profile["kind"]
    modeled = model["kind"] != "uniform"
    order = profile.get("sampling_order", model.get("quadrature_order", 9))
    refined = modeled or "sampling_order" in profile
    if kind == "point":
        offsets = np.zeros((1, 2), dtype=float)
    elif kind == "line":
        length = profile["length_mm"]
        angle = math.radians(profile["angle_deg"])
        count = 2 * order + 7 if refined else 9
        coordinates = length * ((np.arange(count, dtype=float) + 0.5) / count - 0.5)
        offsets = np.column_stack((coordinates * math.cos(angle),
                                   coordinates * math.sin(angle)))
    elif kind == "rectangle":
        # Equal-area midpoint quadrature over the source-local rectangle.
        count = order if refined else 4
        coordinates = (np.arange(count, dtype=float) + 0.5) / count - 0.5
        xx, yy = np.meshgrid(profile["length_mm"] * coordinates,
                            profile["width_mm"] * coordinates)
        angle = math.radians(profile["angle_deg"])
        cos_angle, sin_angle = math.cos(angle), math.sin(angle)
        offsets = np.column_stack((
            (xx * cos_angle - yy * sin_angle).ravel(),
            (xx * sin_angle + yy * cos_angle).ravel(),
        ))
    elif refined and kind in ("disk", "annulus"):
        outer = profile["radius_mm"] if kind == "disk" else profile["outer_radius_mm"]
        inner = 0.0 if kind == "disk" else profile["inner_radius_mm"]
        if kind == "disk":
            # Equal-area grid cells with a central node. Unlike legacy rings,
            # a narrow centered Gaussian can converge toward a central emitter.
            coordinates = outer * 2 * ((np.arange(order) + 0.5) / order - 0.5)
            xx, yy = np.meshgrid(coordinates, coordinates)
            valid = xx * xx + yy * yy <= outer * outer
            offsets = np.column_stack((xx[valid], yy[valid]))
        else:
            # Radial equal-area quadrature never loses a thin annulus on a grid.
            rings = max(2, (order + 1) // 2)
            angles = 4 * order
            offsets = np.concatenate([_ring_points(
                math.sqrt(inner * inner + (outer * outer - inner * inner) * (i + 0.5) / rings),
                angles, phase=math.pi / angles * (i % 2)) for i in range(rings)])
    elif kind == "disk":
        radius = profile["radius_mm"]
        offsets = np.concatenate((
            _ring_points(radius * math.sqrt(1 / 6), 4),
            _ring_points(radius * math.sqrt(2 / 3), 8, phase=math.pi / 8),
        ))
    else:
        inner2 = profile["inner_radius_mm"] ** 2
        span2 = profile["outer_radius_mm"] ** 2 - inner2
        offsets = np.concatenate((
            _ring_points(math.sqrt(inner2 + span2 / 4), 8),
            _ring_points(math.sqrt(inner2 + 3 * span2 / 4), 8,
                         phase=math.pi / 8),
        ))
    weights = np.full(len(offsets), 1.0 / len(offsets), dtype=float)
    if modeled:
        offsets, weights = _emission_footprint(offsets, weights, profile, model, rate)
    positions = np.column_stack((offsets, np.zeros(len(offsets)))) + center
    return positions, weights


_COMMON_MODEL = {"reference_rate_nm_s": 1.0, "rate_exponent_per_decade": 0.0,
                 "valid_rate_min_nm_s": None, "valid_rate_max_nm_s": None, "quadrature_order": 9}
_BOAT_MODEL = {"active_fraction": 1.0, "rate_active_fraction_per_decade": 0.0,
               "center_weight": 0.0, "boat_mode": "open", "cover_height_mm": 10.0,
               "cover_thickness_mm": 0.0, "aperture_kind": "rectangle",
               "aperture_length_mm": 20.0, "aperture_width_mm": 10.0,
               "aperture_radius_mm": 5.0, "aperture_offset_x_mm": 0.0,
               "aperture_offset_y_mm": 0.0, "apertures": None}
_BEAM_MODEL = {"spot_sigma_mm": 3.0, "rate_spot_sigma_per_decade": 0.0,
               "scan_pattern": "point", "scan_width_mm": 0.0, "scan_height_mm": 0.0,
               "dwell_center_fraction": 0.0, "dwell_points": [],
               "scan_center_x_mm": 0.0, "scan_center_y_mm": 0.0, "scan_angle_deg": 0.0}


def validate_emission_model(data, material=None):
    """Validate editable static source-footprint assumptions in mm and nm/s."""
    if not isinstance(data, Mapping):
        raise ValueError("emission_model 必须是 JSON 对象")
    kind = data.get("kind", "uniform")
    if kind not in ("uniform", "resistance_boat", "electron_beam"):
        raise ValueError("发射模型 kind 无效")
    template = dict(_COMMON_MODEL)
    if kind != "uniform":
        template.update(_BOAT_MODEL if kind == "resistance_boat" else _BEAM_MODEL)
    allowed = set(template) | {"kind", "provenance"}
    if set(data) - allowed:
        raise ValueError("发射模型包含未知参数：" + "、".join(sorted(set(data) - allowed)))
    if material is not None and kind != "uniform":
        template.update(material_emission_model(material, kind))
    template.update(data)
    template["kind"] = kind
    template["provenance"] = validate_provenance(template.get("provenance"))
    if type(template["quadrature_order"]) is not int or not 3 <= template["quadrature_order"] <= 21 or template["quadrature_order"] % 2 != 1:
        raise ValueError("源空间采样阶数须为 3–21 的奇数；需自行检查收敛")
    numerical = set(template) - {"quadrature_order", "kind", "provenance", "boat_mode", "aperture_kind",
                                 "scan_pattern", "dwell_points", "apertures"}
    for key in numerical:
        if key.startswith("valid_rate_") and template[key] is None:
            continue
        template[key] = _number(template[key], key)
    # Reuse the shared bounds for material-default and source-override values.
    model_kind = "resistance_boat" if kind == "resistance_boat" else "electron_beam"
    defaults_keys = (set(_COMMON_MODEL) - {"quadrature_order"}) | ({"active_fraction", "rate_active_fraction_per_decade", "center_weight"}
                                         if model_kind == "resistance_boat" else
                                         {"spot_sigma_mm", "rate_spot_sigma_per_decade"})
    validate_model_defaults({k: v for k, v in template.items() if k in defaults_keys}, model_kind)
    if kind == "resistance_boat":
        if template["boat_mode"] not in ("open", "covered"):
            raise ValueError("boat_mode 必须是 open 或 covered")
        if template["aperture_kind"] not in ("rectangle", "disk"):
            raise ValueError("开口须为 rectangle 或 disk")
        for key in ("cover_height_mm", "aperture_length_mm", "aperture_width_mm", "aperture_radius_mm"):
            _size(template[key], key)
        if not 0 <= template["cover_thickness_mm"] <= 10000:
            raise ValueError("盖板厚度须在 0–10000 mm 之间")
        if max(abs(template[k]) for k in ("aperture_offset_x_mm", "aperture_offset_y_mm")) > 10000:
            raise ValueError("开口偏移不得超过 10000 mm")
        apertures = template["apertures"]
        if apertures is None:
            # Preserve old single-hole covers exactly; the explicit list is
            # authoritative after migration and the legacy keys stay readable.
            apertures = [{key: template["aperture_" + key] for key in
                          ("kind", "length_mm", "width_mm", "radius_mm",
                           "offset_x_mm", "offset_y_mm")}]
        if not isinstance(apertures, (list, tuple)) or not 1 <= len(apertures) <= 100:
            raise ValueError("盖板开口须为 1–100 项的几何列表")
        template["apertures"] = [validate_source_aperture(hole) for hole in apertures]
    if kind == "electron_beam":
        if template["scan_pattern"] not in ("point", "line", "raster", "ring", "custom"):
            raise ValueError("扫描轨迹无效")
        for key in ("scan_width_mm", "scan_height_mm"):
            if not 0 <= template[key] <= 10000:
                raise ValueError("扫描范围必须在 0–10000 mm 之间")
        if max(abs(template[k]) for k in ("scan_center_x_mm", "scan_center_y_mm")) > 10000:
            raise ValueError("扫描中心偏移不得超过 10000 mm")
        template["scan_angle_deg"] = (template["scan_angle_deg"] + 180) % 360 - 180
        if not 0 <= template["dwell_center_fraction"] <= 1:
            raise ValueError("中心驻留比例必须在 0–1 之间")
        points = template["dwell_points"]
        if not isinstance(points, (list, tuple)) or len(points) > 100:
            raise ValueError("驻留点须为最多 100 项的列表")
        normalized = []
        for point in points:
            if not isinstance(point, Mapping) or set(point) != {"x_mm", "y_mm", "weight"}:
                raise ValueError("驻留点须包含 x_mm、y_mm、weight")
            point = {key: _number(value, key) for key, value in point.items()}
            if max(abs(point["x_mm"]), abs(point["y_mm"])) > 10000 or not 0 <= point["weight"] <= 1e12:
                raise ValueError("驻留坐标不得超过 10000 mm，权重须为非负有限值且不超过 1e12")
            normalized.append(point)
        if template["scan_pattern"] == "custom" and (not normalized or sum(p["weight"] for p in normalized) <= 0):
            raise ValueError("自定义驻留点必须至少有一个正权重")
        template["dwell_points"] = normalized
    return template


def validate_source_aperture(data):
    """One straight, convex aperture in source-local XY (mm and degrees)."""
    defaults = {"kind": "rectangle", "length_mm": 20.0, "width_mm": 10.0,
                "radius_mm": 5.0, "offset_x_mm": 0.0, "offset_y_mm": 0.0,
                "angle_deg": 0.0}
    if not isinstance(data, Mapping) or set(data) - set(defaults):
        raise ValueError("每个开口须为有效的独立几何对象")
    result = dict(defaults, **data)
    if result["kind"] not in ("rectangle", "disk"):
        raise ValueError("开口须为 rectangle 或 disk")
    for key in set(defaults) - {"kind"}:
        result[key] = _number(result[key], key)
    for key in ("length_mm", "width_mm", "radius_mm"):
        _size(result[key], key)
    if max(abs(result[k]) for k in ("offset_x_mm", "offset_y_mm")) > 10000:
        raise ValueError("开口偏移不得超过 10000 mm")
    result["angle_deg"] = (result["angle_deg"] + 180) % 360 - 180
    return result


def _model_rate(settings, model, explicit=None):
    rate = explicit
    if rate is None:
        rate = getattr(settings, "source_rate_nm_s", None)
    if rate is None:
        rate = getattr(settings, "rate_nm_s", None)
    if rate is None:
        rate = model.get("reference_rate_nm_s", 1.0)
    rate = _number(rate, "沉积速率")
    if not 0 < rate <= 1e9:
        raise ValueError("沉积速率必须为正且不超过 1e9 nm/s")
    low, high = model.get("valid_rate_min_nm_s"), model.get("valid_rate_max_nm_s")
    if (low is not None and rate < low) or (high is not None and rate > high):
        raise ValueError("当前速率超出源模型的有效速率范围；请使用范围内速率或重新标定")
    return rate


def effective_source_exponent(settings, profile=None, rate_nm_s=None):
    """Return the empirical angular exponent at a supplied process rate."""
    profile = validate_source_profile(profile) if profile is not None else None
    model = profile.get("emission_model") if profile else None
    # Legacy caller settings remain authoritative when no emission model exists.
    base = float(getattr(settings, "source_exponent", 1.0))
    if not model:
        return base
    rate = _model_rate(settings, model, rate_nm_s)
    base = profile["source_exponent"]
    value = base + model["rate_exponent_per_decade"] * (math.log10(rate) - math.log10(model["reference_rate_nm_s"]))
    if not 0 <= value <= 20:
        raise ValueError("速率响应后的角指数超出 0–20；请修改速率或标定系数")
    return value


def source_rate_cache_key(profile, rate_nm_s):
    """Rate changes cannot reuse a cached field with a different source shape."""
    profile = validate_source_profile(profile)
    model = profile.get("emission_model")
    if not model:
        return None
    rate = _model_rate(None, model, rate_nm_s)
    return rate if any(value != 0 for key, value in model.items() if key.startswith("rate_") and key.endswith("_per_decade")) else None


def _scan_dwell(model):
    import numpy as np
    kind, width, height = model["scan_pattern"], model["scan_width_mm"], model["scan_height_mm"]
    if kind == "custom":
        points = np.asarray([[p["x_mm"], p["y_mm"]] for p in model["dwell_points"]], dtype=float)
        weights = np.asarray([p["weight"] for p in model["dwell_points"]], dtype=float)
    elif kind == "point":
        points, weights = np.zeros((1, 2)), np.ones(1)
    elif kind == "line":
        points = np.column_stack((np.linspace(-width / 2, width / 2, 9), np.zeros(9)))
        weights = np.ones(9)
    elif kind == "ring":
        angles = 2 * math.pi * np.arange(16) / 16
        points = np.column_stack((width / 2 * np.cos(angles), height / 2 * np.sin(angles)))
        weights = np.ones(16)
    else:
        xx, yy = np.meshgrid(np.linspace(-width / 2, width / 2, 5), np.linspace(-height / 2, height / 2, 5))
        points = np.column_stack((xx.ravel(), yy.ravel()))
        weights = np.ones(25)
    weights = weights / weights.sum()
    center_fraction = model["dwell_center_fraction"]
    if center_fraction:
        points = np.concatenate((points, np.zeros((1, 2))))
        weights = np.concatenate((weights * (1 - center_fraction), [center_fraction]))
    angle = math.radians(model["scan_angle_deg"])
    c, s = math.cos(angle), math.sin(angle)
    points = points @ np.asarray([[c, s], [-s, c]])
    points += np.asarray([model["scan_center_x_mm"], model["scan_center_y_mm"]])
    return points, weights


def _emission_footprint(offsets, weights, profile, model, rate):
    """Static spatial proxy; weights are normalized BEFORE cover interception."""
    import numpy as np
    response = (math.log10(rate) - math.log10(model["reference_rate_nm_s"]))
    if model["kind"] == "resistance_boat":
        fraction = model["active_fraction"] + model["rate_active_fraction_per_decade"] * response
        if not 0 < fraction <= 1:
            raise ValueError("速率响应后的有效尺寸比例须在 0–1 之间")
        offsets = offsets * fraction
        if model["center_weight"]:
            radii2 = np.sum(offsets ** 2, axis=1)
            scale = max(float(np.max(radii2)), 1e-30)
            weights = weights * ((1 - model["center_weight"]) + model["center_weight"] * np.exp(-2 * radii2 / scale))
    elif model["kind"] == "electron_beam":
        sigma = model["spot_sigma_mm"] * (1 + model["rate_spot_sigma_per_decade"] * response)
        if not 0 < sigma <= 10000:
            raise ValueError("速率响应后的等效发射高斯 σ 须在 0–10000 mm 之间")
        angle = math.radians(profile.get("angle_deg", 0))
        local = np.column_stack((offsets[:, 0] * math.cos(angle) + offsets[:, 1] * math.sin(angle),
                                  -offsets[:, 0] * math.sin(angle) + offsets[:, 1] * math.cos(angle)))
        dwell, dwell_weights = _scan_dwell(model)
        # Log-sum-exp avoids silent all-zero normalization for narrow Gaussians.
        positive = dwell_weights > 0
        exponents = -0.5 * np.sum(((local[:, None, :] - dwell[None, positive, :]) / sigma) ** 2, axis=2)
        exponents += np.log(dwell_weights[positive])[None, :]
        peak = float(np.max(exponents))
        weights = weights * np.sum(np.exp(exponents - peak), axis=1)
    total = float(weights.sum())
    if not math.isfinite(total) or total <= 0:
        raise ValueError("源发射区域没有可用权重；请调整足迹或采样尺寸")
    return offsets, weights / total


def source_cover(profile, settings):
    """A raised source-local aperture, separate from machine correction boards."""
    if profile is None:
        return None
    profile = validate_source_profile(profile)
    model = profile.get("emission_model", {})
    if model.get("kind") != "resistance_boat" or model.get("boat_mode") != "covered":
        return None
    return dict(model, source_x_mm=float(settings.source_x_mm),
                source_y_mm=float(settings.source_y_mm), source_z_mm=float(settings.source_z_mm),
                angle_deg=profile.get("angle_deg", 0.0))


def source_visibility(xp, x, y, z, sx, sy, sz, cover):
    """Ballistic LOS through both faces of a straight raised aperture channel.

    Receiver dependence is essential: an emitter is never removed wholesale.
    Intercepted weight is lost; the transmitted weights are never renormalized.
    """
    if cover is None:
        return xp.zeros_like(x, dtype=bool) | True
    angle = math.radians(cover["angle_deg"])
    ca, sa = math.cos(angle), math.sin(angle)
    planes = []
    heights = (cover["cover_height_mm"],)
    if cover["cover_thickness_mm"]:
        heights += (cover["cover_height_mm"] + cover["cover_thickness_mm"],)
    for height in heights:
        plane = cover["source_z_mm"] + height
        dz = z - sz
        # A parallel ray cannot cross a raised cover plane.
        t = (plane - sz) / xp.where(dz == 0, 1.0, dz)
        ix = sx + (x - sx) * t - cover["source_x_mm"]
        iy = sy + (y - sy) * t - cover["source_y_mm"]
        planes.append((ix * ca + iy * sa, -ix * sa + iy * ca,
                       (dz == 0) | (t <= 0) | (t >= 1)))
    holes = cover.get("apertures")
    if holes is None:
        holes = [{key: cover["aperture_" + key] for key in
                  ("kind", "length_mm", "width_mm", "radius_mm",
                   "offset_x_mm", "offset_y_mm")}]
    visible = xp.zeros_like(x, dtype=bool)
    for hole in holes:
        # Union of straight channels, not independent unions at the two faces:
        # entering hole A and leaving hole B crosses an absorbing wall.
        through_hole = xp.zeros_like(x, dtype=bool) | True
        angle = math.radians(hole.get("angle_deg", 0.0))
        ch, sh = math.cos(angle), math.sin(angle)
        for local_x, local_y, outside_segment in planes:
            dx, dy = local_x - hole["offset_x_mm"], local_y - hole["offset_y_mm"]
            u, v = dx * ch + dy * sh, -dx * sh + dy * ch
            if hole["kind"] == "disk":
                inside = u * u + v * v <= hole["radius_mm"] ** 2
            else:
                inside = ((xp.abs(u) <= hole["length_mm"] / 2) &
                          (xp.abs(v) <= hole["width_mm"] / 2))
            through_hole = through_hole & (outside_segment | inside)
        visible = visible | through_hole
    return visible
