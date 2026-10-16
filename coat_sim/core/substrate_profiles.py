"""Standalone substrate outline and physical surface profiles.

All coordinates and surface coefficients use millimetres. The surface centre
is h(0)=0; a positive radius of curvature puts the rim toward machine +Z.
"""

from .data_files import METADATA_FIELDS, design_metadata
from .design_files import validate_design_path

from collections.abc import Mapping
import json
import math
from pathlib import Path

from .substrate_surface import validate_substrate_surface


SUBSTRATE_SCHEMA_VERSION = 1


def _finite(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label}必须为有限数值")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须为有限数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}必须为有限数值")
    return number


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_intersect(a, b, c, d):
    scale = max(*(abs(v) for point in (a, b, c, d) for v in point), 1.0)
    tolerance = 1e-10 * scale * scale
    ab_c, ab_d = _cross(a, b, c), _cross(a, b, d)
    cd_a, cd_b = _cross(c, d, a), _cross(c, d, b)
    def on_segment(p, q, point):
        return (min(p[0], q[0]) - tolerance <= point[0] <= max(p[0], q[0]) + tolerance
                and min(p[1], q[1]) - tolerance <= point[1] <= max(p[1], q[1]) + tolerance)
    if abs(ab_c) <= tolerance and on_segment(a, b, c):
        return True
    if abs(ab_d) <= tolerance and on_segment(a, b, d):
        return True
    if abs(cd_a) <= tolerance and on_segment(c, d, a):
        return True
    if abs(cd_b) <= tolerance and on_segment(c, d, b):
        return True
    return (ab_c > tolerance) != (ab_d > tolerance) and (cd_a > tolerance) != (cd_b > tolerance)


def _vertices(value, label, maximum):
    if not isinstance(value, (list, tuple)) or not 3 <= len(value) <= maximum:
        raise ValueError(f"{label}须有 3–{maximum} 个点")
    points = []
    for index, point in enumerate(value):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"{label}第 {index + 1} 个点须为 [x, y]")
        points.append([_finite(point[0], f"{label} X"),
                       _finite(point[1], f"{label} Y")])
    return points


def _has_evenodd_area(vertices, radius):
    """Detect positive filled area, including self-crossing outlines.

    Between consecutive vertex Y levels, odd-even crossings are paired on
    a horizontal scanline. A positive span at an interior scanline implies
    a positive-area neighbourhood. Degenerate retraced loops have no span.
    """
    levels = sorted(set(point[1] for point in vertices))
    width_tolerance = 1e-9 * max(radius, 1.0)
    edges = list(zip(vertices, vertices[1:] + vertices[:1]))
    for lower, upper in zip(levels, levels[1:]):
        if upper <= lower:
            continue
        y = (lower + upper) / 2.0
        crossings = sorted(
            ax + (y - ay) * (bx - ax) / (by - ay)
            for (ax, ay), (bx, by) in edges
            if (ay > y) != (by > y)
        )
        for left, right in zip(crossings[::2], crossings[1::2]):
            if right - left > width_tolerance:
                return True
    return False


def _simple_polygon(vertices, *, allow_self_intersection=False):
    radius = max(math.hypot(*point) for point in vertices)
    if not 0 < radius <= 10000:
        raise ValueError("轮廓各点到盘心距离须在 0–10000 mm 范围内")
    area_twice = sum(a[0] * b[1] - b[0] * a[1]
                     for a, b in zip(vertices, vertices[1:] + vertices[:1]))
    area_tolerance = 1e-9 * max(radius * radius, 1)
    if allow_self_intersection:
        # Signed shoelace area can cancel for a bow tie, so test the actual
        # even-odd fill for a positive interior span instead.
        if not _has_evenodd_area(vertices, radius):
            raise ValueError("曲线轮廓面积过小或为零")
    elif abs(area_twice) <= area_tolerance:
        raise ValueError("轮廓面积过小或为零")
    count = len(vertices)
    for i in range(count):
        a, b = vertices[i], vertices[(i + 1) % count]
        if math.dist(a, b) <= 1e-9:
            raise ValueError("轮廓相邻点不可重合")
        if not allow_self_intersection:
            for j in range(i + 2, count):
                if i == 0 and j == count - 1:
                    continue
                if _segments_intersect(a, b, vertices[j], vertices[(j + 1) % count]):
                    raise ValueError("多边形边不可相交")
    return radius


def sample_curve_points(control_points):
    """Five Catmull–Rom samples per closed control segment, in local mm."""
    controls = _vertices(control_points, "曲线控制点", 100)
    result = []
    count = len(controls)
    for i in range(count):
        p0, p1, p2, p3 = (controls[(i - 1) % count], controls[i],
                          controls[(i + 1) % count], controls[(i + 2) % count])
        for sample in range(5):
            t = sample / 5.0
            result.append([
                0.5 * ((2 * p1[axis]) + (-p0[axis] + p2[axis]) * t +
                       (2 * p0[axis] - 5 * p1[axis] + 4 * p2[axis] - p3[axis]) * t * t +
                       (-p0[axis] + 3 * p1[axis] - 3 * p2[axis] + p3[axis]) * t * t * t)
                for axis in range(2)])
    return result


def outline_polygon_points(outline):
    """Return material outline vertices for polygon, rectangle, or curve."""
    kind = outline.get("kind")
    if kind == "polygon":
        return outline["points_mm"]
    if kind == "rectangle":
        half_w, half_h = outline["width_mm"] / 2, outline["height_mm"] / 2
        return [[-half_w, -half_h], [half_w, -half_h],
                [half_w, half_h], [-half_w, half_h]]
    if kind == "curve":
        return sample_curve_points(outline["control_points_mm"])
    raise ValueError("圆形轮廓没有多边形顶点")


def validate_substrate_profile(profile):
    """Return a canonical JSON-compatible v1 substrate profile.

    Raises ValueError for malformed, unsupported, or non-physical geometry.
    """
    if not isinstance(profile, Mapping):
        raise ValueError("基片配置必须为 JSON 对象")
    if set(profile) - METADATA_FIELDS - {"material"} != {"schema_version", "name", "outline", "surface"}:
        raise ValueError("基片配置须包含 schema_version、name、outline、surface")
    version = profile["schema_version"]
    if isinstance(version, bool) or version != SUBSTRATE_SCHEMA_VERSION:
        raise ValueError("基片文件版本须为 1")
    name = profile["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("基片名称须为 1–120 个字符")
    outline = profile["outline"]
    if not isinstance(outline, Mapping):
        raise ValueError("基片轮廓必须为对象")
    kind = outline.get("kind")
    if kind == "circle":
        if set(outline) != {"kind", "radius_mm"}:
            raise ValueError("圆形轮廓须包含 kind 和 radius_mm")
        radius = _finite(outline["radius_mm"], "基片半径")
        if not 0 < radius <= 10000:
            raise ValueError("基片半径须在 0–10000 mm 范围内")
        normalized_outline = {"kind": "circle", "radius_mm": radius}
    elif kind == "rectangle":
        if set(outline) != {"kind", "width_mm", "height_mm"}:
            raise ValueError("矩形轮廓须包含 kind、width_mm 和 height_mm")
        width = _finite(outline["width_mm"], "矩形长")
        height = _finite(outline["height_mm"], "矩形宽")
        if width <= 0 or height <= 0 or math.hypot(width / 2, height / 2) > 10000:
            raise ValueError("矩形长宽须为正数，外接半径不得超过 10000 mm")
        radius = math.hypot(width / 2, height / 2)
        normalized_outline = {"kind": "rectangle", "width_mm": width,
                              "height_mm": height}
    elif kind == "polygon":
        if set(outline) != {"kind", "points_mm"}:
            raise ValueError("多边形轮廓须包含 kind 和 points_mm")
        vertices = _vertices(outline["points_mm"], "多边形顶点", 500)
        radius = _simple_polygon(vertices)
        normalized_outline = {"kind": "polygon", "points_mm": vertices}
    elif kind == "curve":
        if set(outline) != {"kind", "control_points_mm"}:
            raise ValueError("曲线轮廓须包含 kind 和 control_points_mm")
        controls = _vertices(outline["control_points_mm"], "曲线控制点", 100)
        samples = sample_curve_points(controls)
        radius = _simple_polygon(samples, allow_self_intersection=True)
        normalized_outline = {"kind": "curve", "control_points_mm": controls}
    else:
        raise ValueError("基片轮廓须为 circle、rectangle、polygon 或 curve")
    surface = validate_substrate_surface(profile["surface"], radius)
    if surface["kind"] == "paraboloid":
        raise ValueError("基片文件曲面须为 flat、spherical 或 asphere")
    if surface["kind"] != "flat" and kind != "circle":
        raise ValueError("球面和非球面须采用圆形轮廓")
    normalized = {"schema_version": 1, "name": name.strip(),
                  "outline": normalized_outline, "surface": surface, **design_metadata(profile)}
    if profile.get("material") is not None:
        from .materials import validate_material_profile, substrate_bindable
        material = validate_material_profile(profile["material"])
        if not substrate_bindable(material):
            raise ValueError("基片只能选用基片用途的材料")
        normalized["material"] = material
    return normalized


def new_substrate_profile():
    return {"schema_version": 1, "name": "新建基片",
            "outline": {"kind": "circle", "radius_mm": 150.0},
            "surface": {"kind": "flat"}}


def effective_radius_mm(profile):
    outline = validate_substrate_profile(profile)["outline"]
    if outline["kind"] == "circle":
        return outline["radius_mm"]
    if outline["kind"] == "rectangle":
        return math.hypot(outline["width_mm"] / 2, outline["height_mm"] / 2)
    return max(math.hypot(*point) for point in outline_polygon_points(outline))


def substrate_mask(x, y, profile):
    """Sample the physical outline at substrate-local coordinates in mm.

    The polygon uses an even-odd rule. Its exact boundary is inside.
    """
    import numpy as np

    outline = validate_substrate_profile(profile)["outline"]
    xx, yy = np.broadcast_arrays(np.asarray(x, dtype=float), np.asarray(y, dtype=float))
    finite = np.isfinite(xx) & np.isfinite(yy)
    if outline["kind"] == "circle":
        radius = outline["radius_mm"]
        return finite & (xx * xx + yy * yy <= radius * radius * (1 + 1e-12))
    if outline["kind"] == "rectangle":
        return (finite &
                (np.abs(xx) <= outline["width_mm"] / 2 + 1e-9) &
                (np.abs(yy) <= outline["height_mm"] / 2 + 1e-9))
    inside = np.zeros(xx.shape, dtype=bool)
    boundary = np.zeros(xx.shape, dtype=bool)
    points = outline_polygon_points(outline)
    for index, (bx, by) in enumerate(points):
        ax, ay = points[index - 1]
        cross = (xx - ax) * (by - ay) - (yy - ay) * (bx - ax)
        scale = max(abs(ax), abs(ay), abs(bx), abs(by), 1.0)
        epsilon = 1e-10 * scale * scale
        boundary |= ((np.abs(cross) <= epsilon) &
                     (xx >= min(ax, bx) - 1e-9) & (xx <= max(ax, bx) + 1e-9) &
                     (yy >= min(ay, by) - 1e-9) & (yy <= max(ay, by) + 1e-9))
        if ay != by:
            inside ^= ((ay > yy) != (by > yy)) & (xx < ax + (yy - ay) * (bx - ax) / (by - ay))
    return finite & (inside | boundary)


def read_substrate_profile(path):
    validate_design_path("substrate", path)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取基片文件：{exc}") from exc
    return validate_substrate_profile(data)


def write_substrate_profile(path, profile):
    validate_design_path("substrate", path)
    canonical = validate_substrate_profile(profile)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(canonical, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return canonical
