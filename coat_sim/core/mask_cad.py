"""Portable 2-D mask CAD exchange and dimensioned PDF drawings.

DXF geometry is flattened into the existing one-outer/multiple-hole mask model.
No DWG, solid modelling, machine control, or manufacturing certification is implied.
Path authorization belongs to the service; this module owns overwrite policy.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime
from itertools import islice
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from .atomic_io import replace_with_retry
from .machine_profiles import validate_mask_profile

MAX_POINTS = 500
MAX_ENTITIES = 20000
MAX_DXF_BYTES = 64 * 1024 * 1024
_EPS = 1e-8
_ANNOTATIONS = frozenset({"TEXT", "MTEXT", "DIMENSION", "LEADER", "MLEADER", "POINT"})


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}须为有限数值")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}须为有限数值") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{label}须为{'正的' if positive else ''}有限数值")
    return number


def _distance(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _xy(vertex, scale: float) -> list[float]:
    xyz = tuple(vertex)
    x, y = float(xyz[0]) * scale, float(xyz[1]) * scale
    z = float(xyz[2]) * scale if len(xyz) > 2 else 0.0
    if not all(math.isfinite(n) for n in (x, y, z)) or max(abs(x), abs(y)) > 1e9:
        raise ValueError("CAD坐标无效或超出本项目二维交换范围（±10^9 mm）")
    if abs(z) > _EPS:
        raise ValueError("只支持机架XY平面、CAD Z=0的二维轮廓；不要投影三维实体")
    return [x, y]


def _normal(entity) -> None:
    extrusion = tuple(_number(value, "CAD法向") for value in entity.dxf.get("extrusion", (0, 0, 1)))
    if len(extrusion) != 3:
        raise ValueError("CAD法向须为三维向量")
    if abs(extrusion[0]) > _EPS or abs(extrusion[1]) > _EPS or abs(abs(extrusion[2]) - 1) > _EPS:
        raise ValueError(f"{entity.dxftype()}不是XY平面的二维实体")
    if entity.dxf.hasattr("thickness") and abs(_number(entity.dxf.get("thickness", 0), "CAD实体挤出厚度")) > _EPS:
        raise ValueError("DXF实体带挤出厚度；请提供二维边界，不将其投影成平面")


def _bounded_vertices(vertices, scale: float) -> list[list[float]]:
    points = [_xy(v, scale) for v in islice(vertices, MAX_POINTS + 2)]
    if len(points) > MAX_POINTS + 1:
        raise ValueError("曲线采样超过500点；请增加曲线误差或在CAD中分配更简洁的轮廓，不会自动简化")
    if len(points) < 2:
        raise ValueError("CAD实体没有可用边界")
    return points


def _arc_points(entity, scale: float, tolerance: float, *, circle: bool) -> list[list[float]]:
    _normal(entity)
    radius = _number(entity.dxf.radius, "圆弧半径", positive=True) * scale
    _xy(entity.ocs().to_wcs(entity.dxf.center), scale)
    from ezdxf.math import arc_angle_span_deg
    start = 0.0 if circle else _number(entity.dxf.start_angle, "圆弧起始角")
    end = 360.0 if circle else _number(entity.dxf.end_angle, "圆弧终止角")
    span = 360.0 if circle else arc_angle_span_deg(start, end)
    if not math.isfinite(span) or span <= 0:
        raise ValueError("圆弧起终角重合或跨度无效；零弧不会当作整圆导入")
    # Stable form of sagitta = r*(1-cos(delta/2)); no Bezier display approximation.
    delta = min(math.pi, 4 * math.asin(math.sqrt(min(1.0, tolerance / (2 * radius)))))
    if delta <= 0:
        raise ValueError("圆弧采样精度低于浮点可表示范围")
    count = max(3 if span == 360.0 else 1, math.ceil(math.radians(span) / delta))
    if count > MAX_POINTS:
        raise ValueError("圆弧按指定弦差需要超过500段；请增大曲线误差，不会静默简化")
    return _bounded_vertices(entity.vertices(start + span * i / count for i in range(count + 1)), scale)


def _entity_points(entity, scale: float, tolerance: float) -> tuple[list[list[float]], bool]:
    kind = entity.dxftype()
    if kind == "LINE":
        _normal(entity)
        return [_xy(entity.dxf.start, scale), _xy(entity.dxf.end, scale)], False
    if kind in ("ARC", "CIRCLE"):
        return _arc_points(entity, scale, tolerance, circle=kind == "CIRCLE"), True
    if kind in ("LWPOLYLINE", "POLYLINE"):
        _normal(entity)
        if kind == "POLYLINE" and not entity.is_2d_polyline:
            raise ValueError("不支持三维POLYLINE、网格或多面体")
        if kind == "LWPOLYLINE":
            if entity.has_width:
                raise ValueError("不支持带宽LWPOLYLINE；请导出其真实外边界")
            elevation = _number(entity.dxf.elevation, "折线高程")
            vertices = [(float(x), float(y), _number(bulge, "折线bulge"))
                        for x, y, bulge in entity.get_points("xyb")]
            closed = entity.closed
        else:
            if any(entity.dxf.hasattr(key) and abs(_number(entity.dxf.get(key, 0), "折线宽度")) > _EPS
                   for key in ("default_start_width", "default_end_width")):
                raise ValueError("不支持带宽POLYLINE；请导出其真实外边界")
            vertices = []
            for vertex in entity.vertices:
                if any(abs(_number(vertex.dxf.get(key, 0), "折线宽度")) > _EPS
                       for key in ("start_width", "end_width")):
                    raise ValueError("不支持带宽POLYLINE；请导出其真实外边界")
                # ezdxf's virtual 2-D entities ignore the per-vertex Z value;
                # explicitly reject invalid/non-planar input before that projection.
                location = tuple(vertex.dxf.location)
                _xy(location, scale)
                vertices.append((float(location[0]), float(location[1]),
                                 _number(vertex.dxf.get("bulge", 0), "折线bulge")))
            elevation_xyz = tuple(_number(value, "折线高程")
                                  for value in entity.dxf.get("elevation", (0, 0, 0)))
            if len(elevation_xyz) != 3:
                raise ValueError("POLYLINE高程须为三维向量")
            elevation = elevation_xyz[2]
            closed = entity.is_closed
        if not 2 <= len(vertices) <= MAX_POINTS + 1:
            raise ValueError("空折线或折线原始顶点超过501点；不会自动简化")
        ocs = entity.ocs()
        expected = [_xy(ocs.to_wcs((x, y, elevation)), scale) for x, y, _ in vertices]
        segment_count = len(vertices) if closed else len(vertices) - 1
        primitives = iter(entity.virtual_entities())
        points: list[list[float]] = []
        curved = False
        for index in range(segment_count):
            a = expected[index]
            b = expected[(index + 1) % len(expected)]
            bulge = vertices[index][2]
            primitive = next(primitives, None)
            if primitive is None:
                raise ValueError("折线有退化或无法解析的线段；不会跳过几何")
            if _distance(a, b) <= _EPS:
                # A closed DXF may encode its first vertex once more at the end.
                # Only that redundant straight closing segment can be omitted.
                if closed and index == segment_count - 1 and bulge == 0 and expected[-1] == expected[0]:
                    continue
                raise ValueError("折线包含零长度边或退化圆弧")
            part, has_curve = _entity_points(primitive, scale, tolerance)
            # Virtual ARC is always CCW, even when a negative bulge describes
            # clockwise traversal.  Direct it from the original segment start,
            # including when the first segment has negative bulge.
            if bulge < 0:
                part.reverse()
            endpoint_error = max(_distance(part[0], a), _distance(part[-1], b))
            roundoff = max(_EPS, 16 * max(math.ulp(value) for point in (a, b) for value in point))
            if endpoint_error > roundoff:
                raise ValueError("折线虚拟线段与原始起终点不一致")
            part[0], part[-1] = a[:], b[:]
            curved |= has_curve
            if points and _distance(points[-1], part[0]) > _EPS:
                raise ValueError("折线内部几何不连续")
            points.extend(part[1:] if points else part)
            if len(points) > MAX_POINTS + 1:
                raise ValueError("折线按指定弦差采样超过500点；不会自动简化")
        if next(primitives, None) is not None:
            raise ValueError("折线虚拟线段数与原始顶点不一致")
        if len(points) < 2:
            raise ValueError("空折线或退化折线")
        return points, curved
    if kind == "ELLIPSE":
        _normal(entity)
        _xy(entity.dxf.center, scale)
        _xy(entity.dxf.major_axis, scale)
        return _bounded_vertices(entity.flattening(tolerance / scale, segments=8), scale), True
    if kind == "SPLINE":
        _normal(entity)
        for point in (*entity.control_points, *entity.fit_points):
            _xy(point, scale)
        return _bounded_vertices(entity.flattening(tolerance / scale, segments=4), scale), True
    raise ValueError(f"不支持DXF几何实体 {kind}；块、三维/实体、填充和未处理几何不能直接导入")


def _stitch(pieces: list[tuple[list[list[float]], bool]], join: float, curve_error: float):
    nodes: list[list[float]] = []
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    adjacent: dict[int, list[int]] = defaultdict(list)
    edges = []
    welded = 0

    def node(point):
        cell = (math.floor(point[0] / join), math.floor(point[1] / join))
        matches = [i for x in range(cell[0] - 1, cell[0] + 2)
                   for y in range(cell[1] - 1, cell[1] + 2)
                   for i in buckets[(x, y)] if _distance(nodes[i], point) <= join]
        if len(matches) > 1:
            raise ValueError("连接容差内有多个候选端点，连接关系不唯一；请降低连接容差或整理CAD")
        if matches:
            return matches[0]
        index = len(nodes)
        nodes.append(point[:])
        buckets[cell].append(index)
        return index

    for points, curved in pieces:
        a, b = node(points[0]), node(points[-1])
        displacement = max(_distance(points[0], nodes[a]), _distance(points[-1], nodes[b]))
        if displacement > _EPS:
            welded += 1
        adjusted = [nodes[a][:], *[p[:] for p in points[1:-1]], nodes[b][:]]
        index = len(edges)
        edges.append((a, b, adjusted, (curve_error if curved else 0.0) + displacement))
        adjacent[a].append(index)
        adjacent[b].append(index)
    if any(len(entries) != 2 for entries in adjacent.values()):
        raise ValueError("边界未闭合、相切端点或分支：每个连接点须恰好连接两条边；请检查连接容差")
    seen: set[int] = set()
    loops, errors = [], []
    for first in range(len(edges)):
        if first in seen:
            continue
        start, current, points, error = edges[first]
        points = [p[:] for p in points]
        seen.add(first)
        while current != start:
            choices = [i for i in adjacent[current] if i not in seen]
            if len(choices) != 1:
                raise ValueError("边界链出现重复边或分支，不能形成唯一闭合轮廓")
            index = choices[0]
            a, b, part, part_error = edges[index]
            if a != current:
                part = list(reversed(part))
                a, b = b, a
            points.extend(part[1:])
            error = max(error, part_error)
            seen.add(index)
            current = b
            if len(points) > MAX_POINTS + 1:
                raise ValueError("闭合轮廓超过500点；现有修正板模型不支持，不会自动简化")
        if _distance(points[0], points[-1]) > _EPS:
            raise ValueError("闭合轮廓端点不一致")
        points.pop()  # Repeated DXF closing endpoint is not an additional model vertex.
        if not 3 <= len(points) <= MAX_POINTS:
            raise ValueError("每个闭合轮廓须有3–500个顶点")
        loops.append(points)
        errors.append(error)
    if len(loops) > 31:
        raise ValueError("现有修正板模型最多支持一个外轮廓和30个孔洞")
    return loops, errors, welded


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _point_segment_distance(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    denominator = dx * dx + dy * dy
    if denominator == 0:
        return _distance(p, a)
    fraction = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / denominator))
    return _distance(p, (a[0] + fraction * dx, a[1] + fraction * dy))


def _segments_close(a, b, c, d, margin=0.0):
    eps = max(_EPS, margin)
    if (max(a[0], b[0]) + eps < min(c[0], d[0]) or max(c[0], d[0]) + eps < min(a[0], b[0])
            or max(a[1], b[1]) + eps < min(c[1], d[1]) or max(c[1], d[1]) + eps < min(a[1], b[1])):
        return False
    crosses = (_cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b))
    if crosses[0] * crosses[1] < 0 and crosses[2] * crosses[3] < 0:
        return True
    return min(_point_segment_distance(a, c, d), _point_segment_distance(b, c, d),
               _point_segment_distance(c, a, b), _point_segment_distance(d, a, b)) <= eps


def _inside(point, polygon):
    result = False
    x, y = point
    for a, b in zip(polygon, polygon[1:] + polygon[:1]):
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            result = not result
    return result


def _area(polygon):
    origin = polygon[0]
    return math.fsum(_cross(origin, a, b) for a, b in zip(polygon, polygon[1:] + polygon[:1])) / 2


def _classify(loops, errors=None):
    errors = errors or [0.0] * len(loops)
    if not loops:
        raise ValueError("DXF没有可用的闭合轮廓")
    edges = [list(zip(poly, poly[1:] + poly[:1])) for poly in loops]
    for index, poly in enumerate(loops):
        if abs(_area(poly)) < 1e-9:
            raise ValueError("轮廓面积为零")
        if any(_distance(a, b) <= _EPS for a, b in edges[index]):
            raise ValueError("轮廓有重复顶点或零长度边")
        for i, (a, b) in enumerate(edges[index]):
            previous = poly[(i - 1) % len(poly)]
            if _point_segment_distance(b, previous, a) <= _EPS:
                raise ValueError("相邻轮廓边折返重叠")
            for j in range(i + 2, len(poly)):
                if i == 0 and j == len(poly) - 1:
                    continue
                c, d = edges[index][j]
                if _segments_close(a, b, c, d, errors[index] * 2):
                    raise ValueError("轮廓自交/自切，或细节小于曲线近似误差包络；请检查CAD或降低曲线误差")
    for i in range(len(loops)):
        for j in range(i + 1, len(loops)):
            if any(_segments_close(a, b, c, d, errors[i] + errors[j])
                   for a, b in edges[i] for c, d in edges[j]):
                raise ValueError("轮廓相交/相切，或间隙小于曲线近似误差包络；请检查CAD或降低误差")
    parents = [[j for j, other in enumerate(loops) if j != i and _inside(poly[0], other)]
               for i, poly in enumerate(loops)]
    roots = [i for i, containers in enumerate(parents) if not containers]
    if len(roots) != 1:
        raise ValueError("需要恰好一个外轮廓；多个分离板体不适用于现有修正板模型")
    outer_index = roots[0]
    if any(containers != [outer_index] for i, containers in enumerate(parents) if i != outer_index):
        raise ValueError("孔洞须直接位于外轮廓内；不支持嵌套孔/岛或多个层级")
    outer = loops[outer_index]
    holes = [poly for i, poly in enumerate(loops) if i != outer_index]
    outer = outer if _area(outer) > 0 else list(reversed(outer))
    holes = [poly if _area(poly) < 0 else list(reversed(poly)) for poly in holes]
    return outer, holes


def _checked_mask(data: Mapping[str, Any]):
    normalized = validate_mask_profile(dict(data))
    original = [normalized["outer"], *normalized["holes"]]
    outer, holes = _classify(original)
    if outer != normalized["outer"] and outer != list(reversed(normalized["outer"])):
        raise ValueError("孔洞在指定外轮廓之外；请修正板的外轮廓与孔洞关系")
    return {**normalized, "outer": outer, "holes": holes}


def import_mask_dxf(path, *, units="auto", curve_tolerance_mm=0.05,
                    join_tolerance_mm=0.01, base_mask: Mapping[str, Any] | None = None) -> dict:
    """Return a checked draft and import provenance; never save/overwrite JSON."""
    import ezdxf
    from ezdxf import units as dxf_units
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() != ".dxf":
        raise ValueError("仅支持二维DXF；DWG等格式请先在CAD软件中转换")
    if source.stat().st_size > MAX_DXF_BYTES:
        raise ValueError("DXF超过64MiB交换上限")
    tolerance = _number(curve_tolerance_mm, "曲线误差", positive=True)
    join = _number(join_tolerance_mm, "连接容差", positive=True)
    if not 1e-6 <= tolerance <= 1000 or not 1e-6 <= join <= 1000:
        raise ValueError("曲线误差和连接容差须在10^-6至1000 mm之间")
    if units not in ("auto", "mm", "inch"):
        raise ValueError("单位选择须为auto、mm或inch")
    try:
        drawing = ezdxf.readfile(source)
    except ezdxf.DXFError as exc:
        raise ValueError(f"无法解析DXF：{exc}") from exc
    source_code = int(drawing.header.get("$INSUNITS", 0))
    warnings = []
    if units == "auto":
        if source_code == 0:
            raise ValueError("DXF未声明单位；请明确选择mm或inch，不能自动猜测")
        try:
            source_unit = dxf_units.InsertUnits(source_code)
            scale = float(dxf_units.conversion_factor(source_unit, dxf_units.InsertUnits.Millimeters))
            source_units = source_unit.name
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise ValueError("DXF单位不支持，请明确选择mm或inch") from exc
    else:
        scale = 1.0 if units == "mm" else 25.4
        source_units = units
        if source_code not in (0, 4 if units == "mm" else 1):
            warnings.append("手动单位覆盖了DXF的$INSUNITS声明")
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("DXF单位转换无效")
    pieces = []
    ignored = Counter()
    approximated = Counter()
    entity_count = 0
    for entity in drawing.modelspace():
        entity_count += 1
        if entity_count > MAX_ENTITIES:
            raise ValueError("DXF模型空间超过20000个实体，请导出所需二维板轮廓")
        kind = entity.dxftype()
        if kind in _ANNOTATIONS:
            ignored[kind] += 1
            continue
        try:
            points, curved = _entity_points(entity, scale, tolerance)
        except (ezdxf.DXFError, OverflowError, ZeroDivisionError, RecursionError) as exc:
            raise ValueError(f"无法采样{kind}实体：{exc}") from exc
        pieces.append((points, curved))
        if kind in ("ELLIPSE", "SPLINE"):
            approximated[kind] += 1
    loops, errors, welded = _stitch(pieces, join, tolerance)
    outer, holes = _classify(loops, errors)
    data = dict(base_mask or {"z_mm": 300.0})
    data.update(outer=outer, holes=holes)
    data = validate_mask_profile(data)
    if ignored:
        warnings.append("忽略模型空间文字/尺寸/标记实体：" + ", ".join(f"{key}×{count}" for key, count in ignored.items()))
    if approximated:
        warnings.append("椭圆/样条采用ezdxf自适应弦差近似；采样判据不构成全曲线加工精度认证")
    if welded:
        warnings.append(f"{welded}处端点按连接容差合并；最大允许位移{join:g} mm")
    warnings.append("仅导入模型空间二维边界；CAD Z=0，机器板高度保留于z_mm。导入结果为折线，请预览核对后保存")
    return {"data": data, "warnings": warnings, "provenance": {
        "source_path": str(source), "format": "DXF", "source_units": source_units,
        "scale_to_mm": scale, "curve_tolerance_mm": tolerance,
        "join_tolerance_mm": join, "entity_count": entity_count, "welded_endpoints": welded,
        "point_counts": [len(outer), *map(len, holes)]}}


def _target(path, suffix: str, overwrite: bool) -> Path:
    target = Path(path).expanduser().resolve()
    if target.suffix.lower() != suffix:
        raise ValueError(f"目标须使用{suffix}扩展名")
    if not target.parent.is_dir():
        raise ValueError("目标目录不存在")
    if target.exists() and (not overwrite or not target.is_file()):
        raise ValueError("目标文件已存在，请明确允许覆盖" if target.is_file() else "目标不是普通文件")
    return target


def _temporary(target: Path) -> Path:
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".coat-cad-", suffix=target.suffix, delete=False) as handle:
        return Path(handle.name)


def _commit(temporary: Path, target: Path, overwrite: bool) -> None:
    if overwrite:
        replace_with_retry(temporary, target)
        return
    created = False
    try:
        with target.open("xb") as output:
            created = True
            with temporary.open("rb") as source:
                shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise


def export_mask_dxf(data: Mapping[str, Any], path, *, overwrite=False) -> dict:
    import ezdxf
    mask = _checked_mask(data)
    target = _target(path, ".dxf", overwrite)
    drawing = ezdxf.new("R2010")
    drawing.units = 4
    drawing.header["$INSUNITS"] = 4
    drawing.header["$MEASUREMENT"] = 1
    drawing.layers.new("OUTLINE", dxfattribs={"color": 7})
    drawing.layers.new("HOLES", dxfattribs={"color": 2})
    space = drawing.modelspace()
    space.add_lwpolyline(mask["outer"], close=True, dxfattribs={"layer": "OUTLINE"})
    for hole in mask["holes"]:
        space.add_lwpolyline(hole, close=True, dxfattribs={"layer": "HOLES"})
    temporary = _temporary(target)
    try:
        drawing.saveas(temporary)
        _commit(temporary, target, overwrite)
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": str(target), "format": "DXF", "units": "mm", "outer_points": len(mask["outer"]),
            "hole_count": len(mask["holes"]),
            "warnings": ["导出闭合折线、全局XY和毫米单位；未包含机架Z、加工刀路或实体CAD"]}


def _text(value, label, limit=2000):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{label}须为不超过{limit}字符的文字")
    return value


def export_mask_drawing(data: Mapping[str, Any], path, *, options: Mapping[str, Any] | None = None,
                        overwrite=False) -> dict:
    """A fitted dimension drawing, metadata and full polygon coordinate tables."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.figure import Figure
    from .result_pdf import _font, _wrap_line
    mask = _checked_mask(data)
    target = _target(path, ".pdf", overwrite)
    options = dict(options or {})
    accepted = {"paper", "title", "material", "thickness_mm", "tolerance_mm", "notes", "revision", "source", "curve_tolerance_mm"}
    if set(options) - accepted:
        raise ValueError("不认识的加工图选项：" + ", ".join(sorted(set(options) - accepted)))
    paper = options.get("paper", "A4")
    if paper not in ("A4", "A3"):
        raise ValueError("纸张须为A4或A3")
    text = {key: _text(options.get(key, ""), key, 20000 if key == "notes" else 2000)
            for key in ("title", "material", "notes", "revision", "source")}
    dimensions = {key: (_number(options[key], key, positive=True) if options.get(key) is not None else None)
                  for key in ("thickness_mm", "tolerance_mm", "curve_tolerance_mm")}
    font = _font()
    size = (297 / 25.4, 210 / 25.4) if paper == "A4" else (420 / 25.4, 297 / 25.4)
    warnings = ["尺寸基于现有离散轮廓；图纸按纸张适配显示，不是1:1，未生成刀路或认证制造公差"]
    if font.get_family() == ["DejaVu Sans"]:
        warnings.append("系统未找到所支持的中文字体，PDF中文可能缺字；请安装Noto Sans CJK等字体后导出")
    precision = (f"CAD曲线采样判据 {dimensions['curve_tolerance_mm']:g} mm（含自适应近似，不构成制造认证）"
                 if dimensions["curve_tolerance_mm"] is not None else "当前JSON为离散折线；原CAD曲线近似精度未知")
    def shown(key):
        return f"{dimensions[key]:g} mm" if dimensions[key] is not None else "未指定"
    metadata = ["图纸标题：" + (text["title"] or "修正板二维加工图"),
                f"单位：mm；纸张：{paper}横向；版本：{text['revision'] or '未指定'}",
                f"材质：{text['material'] or '未指定'}；板厚：{shown('thickness_mm')}；要求尺寸公差：±{shown('tolerance_mm') if dimensions['tolerance_mm'] is not None else '未指定'}",
                f"来源：{text['source'] or '当前修正板设计'}；机架板高度 z_mm={mask['z_mm']:g}（不作为CAD Z坐标）", precision,
                "尺寸/公差须由设计者与加工方核对；孔位采用包围框与完整顶点坐标，不将折线拟合为精确圆。"]
    if mask.get("description"):
        metadata.append("描述：" + mask["description"])
    if mask.get("notes"):
        metadata.append("设计备注：" + mask["notes"])
    if text["notes"]:
        metadata.append("加工备注：" + text["notes"])
    # Count physical lines before pagination; multi-line descriptions/notes
    # must not hide behind a single wrapped-item count or overflow the sheet.
    lines = [part for entry in metadata for physical_line in (entry.splitlines() or [""])
             for part in (_wrap_line(physical_line, width=100 if paper == "A4" else 145) or [""])]
    loops = [mask["outer"], *mask["holes"]]
    vertices = [("外轮廓" if index == 0 else f"H{index}", n + 1, f"{p[0]:.16g}", f"{p[1]:.16g}")
                for index, polygon in enumerate(loops) for n, p in enumerate(polygon)]
    bounds = lambda poly: (min(p[0] for p in poly), min(p[1] for p in poly), max(p[0] for p in poly), max(p[1] for p in poly))
    xmin, ymin, xmax, ymax = bounds(mask["outer"])
    width, height = xmax - xmin, ymax - ymin
    padding = max(width, height) * 0.10
    page_count = 0
    temporary = _temporary(target)
    title = text["title"] or "修正板二维加工图"
    def figure(title_text):
        fig = Figure(figsize=size)
        FigureCanvasAgg(fig)
        fig.text(0.04, 0.96, title_text, fontproperties=font, fontsize=13, va="top")
        return fig
    def finish(pdf, fig):
        nonlocal page_count
        page_count += 1
        fig.text(0.96, 0.02, f"{page_count} · {datetime.now():%Y-%m-%d}", fontproperties=font, fontsize=7, ha="right")
        pdf.savefig(fig)
        fig.clear()
    try:
        with PdfPages(temporary, metadata={"Title": title, "Creator": "CoatSim", "Subject": "二维轮廓尺寸与坐标；非制造认证"}) as pdf:
            fig = figure(_wrap_line((title.splitlines() or [""])[0], width=88 if paper == "A4" else 125)[0])
            ax = fig.add_axes((0.08, 0.29, 0.64, 0.59))
            for index, poly in enumerate(loops):
                closed = poly + poly[:1]
                ax.plot([p[0] for p in closed], [p[1] for p in closed], color="black" if index == 0 else "#34598c", linewidth=0.9)
                if index:
                    bx0, by0, bx1, by1 = bounds(poly)
                    ax.text((bx0 + bx1) / 2, (by0 + by1) / 2, f"H{index}", fontproperties=font, fontsize=8, ha="center")
            ax.annotate("", xy=(xmin, ymin - padding * .55), xytext=(xmax, ymin - padding * .55), arrowprops={"arrowstyle": "<->", "linewidth": .7})
            ax.text((xmin + xmax) / 2, ymin - padding * .5, f"W = {width:.8g} mm", fontsize=8, ha="center", va="bottom")
            ax.annotate("", xy=(xmax + padding * .55, ymin), xytext=(xmax + padding * .55, ymax), arrowprops={"arrowstyle": "<->", "linewidth": .7})
            ax.text(xmax + padding * .60, (ymin + ymax) / 2, f"H = {height:.8g} mm", fontsize=8, rotation=90, va="center")
            ax.set_xlim(xmin - padding, xmax + padding * 1.25)
            ax.set_ylim(ymin - padding, ymax + padding)
            ax.set_aspect("equal", adjustable="box")
            ax.set_xlabel("X / mm"); ax.set_ylabel("Y / mm")
            ax.tick_params(labelsize=7); ax.grid(alpha=.2)
            side = ["外轮廓包围框", f"X: {xmin:.8g}…{xmax:.8g}", f"Y: {ymin:.8g}…{ymax:.8g}", f"孔数: {len(mask['holes'])}", "", "孔定位/边界完整数据见后页", "图示适配纸张，非1:1"]
            fig.text(.76, .86, "\n".join(side), fontproperties=font, fontsize=8, va="top")
            fig.text(.04, .22, "\n".join(lines[:8]), fontproperties=font, fontsize=7, va="top", linespacing=1.25)
            finish(pdf, fig)
            for offset in range(8, len(lines), 34 if paper == "A4" else 48):
                fig = figure("加工信息与备注（续）")
                count = 34 if paper == "A4" else 48
                fig.text(.04, .89, "\n".join(lines[offset:offset + count]), fontproperties=font, fontsize=8, va="top", linespacing=1.35)
                finish(pdf, fig)
            if mask["holes"]:
                fig = figure("孔定位与边界尺寸 · 全局XY / mm")
                ax = fig.add_axes((.04, .14, .92, .75)); ax.axis("off")
                rows = []
                for index, poly in enumerate(mask["holes"], 1):
                    x0, y0, x1, y1 = bounds(poly)
                    rows.append([f"H{index}", f"{x0:.8g}", f"{y0:.8g}", f"{x1 - x0:.8g}", f"{y1 - y0:.8g}", f"{x0 - xmin:.8g}", f"{y0 - ymin:.8g}"])
                table = ax.table(cellText=rows, colLabels=["孔", "Xmin", "Ymin", "宽", "高", "距外廓Xmin", "距外廓Ymin"], loc="upper center", cellLoc="center")
                table.auto_set_font_size(False); table.set_fontsize(6 if paper == "A4" else 7); table.scale(1, 1.2)
                for cell in table.get_celld().values():
                    cell.set_height(.86 / (len(rows) + 1))
                    cell.get_text().set_fontproperties(font)
                    cell.get_text().set_fontsize(6 if paper == "A4" else 7)
                fig.text(.04, .06, "包围框定位仅描述现有折线；不能当作圆孔直径或几何中心。完整顶点见坐标表。", fontproperties=font, fontsize=8)
                finish(pdf, fig)
            per_column = 32 if paper == "A4" else 46
            for offset in range(0, len(vertices), per_column * 2):
                fig = figure("完整闭合轮廓坐标 · 全局XY / mm")
                for column in range(2):
                    rows = vertices[offset + column * per_column:offset + (column + 1) * per_column]
                    if not rows: continue
                    ax = fig.add_axes((.04 + column * .48, .08, .44, .80)); ax.axis("off")
                    table = ax.table(cellText=rows, colLabels=["轮廓", "点", "X / mm", "Y / mm"], loc="upper center", cellLoc="center")
                    table.auto_set_font_size(False); table.set_fontsize(6 if paper == "A4" else 7); table.scale(1, 1.18)
                    for cell in table.get_celld().values():
                        cell.set_height(.93 / (len(rows) + 1))
                        cell.get_text().set_fontproperties(font)
                        cell.get_text().set_fontsize(6 if paper == "A4" else 7)
                fig.text(.04, .04, "末点与首点闭合；小数为显示舍入，DXF及JSON保留浮点坐标。", fontproperties=font, fontsize=7)
                finish(pdf, fig)
        _commit(temporary, target, overwrite)
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": str(target), "format": "PDF", "pages": page_count, "units": "mm", "warnings": warnings}
