"""Deterministic, offline geometry and process-detail artifacts.

This module draws validated design data returned by :meth:`CoatService.preview`.
It does not calculate deposition, infer missing geometry, or embed remote assets.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
from pathlib import Path
import uuid
from typing import Any, Mapping

from .atomic_io import replace_with_retry

from .data_files import data_path, file_info
from .machine_profiles import validate_mask_profile
from .source_profiles import validate_source_profile
from .substrate_profiles import (effective_radius_mm, outline_polygon_points,
                                 validate_substrate_profile)


_COLORS = {"ink": "#26384a", "blue": "#3478a8", "cyan": "#67b8c7",
           "orange": "#e18a44", "green": "#3d9874", "muted": "#718096",
           "paper": "#f7fafc", "grid": "#d9e2ec", "hole": "#ffffff"}


def _f(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("绘图坐标必须为有限数值")
    return number


def _fmt(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def _label(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(content, encoding="utf-8", newline="\n")
        replace_with_retry(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _artifact_path(kind: str, data: Mapping[str, Any], extension: str) -> Path:
    encoded = json.dumps({"kind": kind, "data": data}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:20]
    return data_path(f"tmp/design-visualizations/{kind}-{digest}.{extension}", root_allowed=False)


def _extent(points: list[tuple[float, float]], fallback: float = 1.0) -> tuple[float, float, float, float]:
    if not points:
        return -fallback, -fallback, fallback, fallback
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _plot_rect(width: float, height: float) -> tuple[float, float, float, float]:
    """Leave a title band and a footer band outside the proportional plot."""
    return (45.0, 85.0, width - 270.0, height - 75.0) if width >= 900 else (
        120.0, 85.0, width - 40.0, height - 75.0)


def _xy_svg(points: list[tuple[float, float]], box: tuple[float, float, float, float],
            width: float, height: float,
            plot_rect: tuple[float, float, float, float] | None = None) -> list[tuple[float, float]]:
    x0, y0, x1, y1 = box
    left, top, right, bottom = plot_rect or _plot_rect(width, height)
    scale = min((right - left) / max(x1 - x0, 1e-6),
                (bottom - top) / max(y1 - y0, 1e-6))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return [((left + right) / 2 + (x - cx) * scale,
             (top + bottom) / 2 - (y - cy) * scale)
            for x, y in points]


def _poly(points: list[tuple[float, float]], box, width, height, *, color=None,
          fill="none", stroke_width=2.0, close=True, dash=None, plot_rect=None) -> str:
    mapped = _xy_svg(points, box, width, height, plot_rect)
    coords = " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in mapped)
    tag = "polygon" if close else "polyline"
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<{tag} points="{coords}" fill="{fill}" fill-rule="evenodd" '
            f'stroke="{color or _COLORS["blue"]}" stroke-width="{stroke_width}"'
            f'{dash_attr}/>' )


def _circle(cx: float, cy: float, radius: float, box, width, height, *, color, fill="none",
            stroke_width=2.0, dash=None, plot_rect=None) -> str:
    center, edge = _xy_svg([(cx, cy), (cx + radius, cy)], box, width, height, plot_rect)
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<circle cx="{_fmt(center[0])}" cy="{_fmt(center[1])}" '
            f'r="{_fmt(math.dist(center, edge))}" fill="{fill}" stroke="{color}" '
            f'stroke-width="{stroke_width}"{dash_attr}/>' )


def _header(title: str, subtitle: str, width=800, height=560) -> list[str]:
    return [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{_label(title)}">',
            '<rect width="100%" height="100%" fill="#fff"/>',
            f'<text x="28" y="34" font-family="sans-serif" font-size="21" font-weight="700" fill="{_COLORS["ink"]}">{_label(title)}</text>',
            f'<text x="28" y="58" font-family="sans-serif" font-size="12" fill="{_COLORS["muted"]}">{_label(subtitle)}</text>']


def _footer(y=540) -> str:
    return f'<text x="28" y="{y}" font-family="sans-serif" font-size="11" fill="#718096">几何示意图，坐标与单位见标注；不表示制造公差或设备实测。</text></svg>'


def _mask_svg(profile: Mapping[str, Any], title="修正板俯视图") -> str:
    mask = validate_mask_profile(dict(profile))
    rings = [[tuple(map(_f, p)) for p in mask["outer"]]] + [
        [tuple(map(_f, p)) for p in hole] for hole in mask["holes"]]
    box = _extent([p for ring in rings for p in ring])
    out = _header(title, f"机架 XY / mm · 板高 z = {_fmt(mask['z_mm'])} mm")
    for index, ring in enumerate(rings):
        out.append(_poly(ring, box, 800, 560, color=_COLORS["blue"] if index == 0 else _COLORS["orange"],
                         fill="rgba(52,120,168,.15)" if index == 0 else "#ffffff",
                         stroke_width=2.4 if index == 0 else 1.8))
    out.append(_axis_legend("蓝色外轮廓 / 白色孔洞"))
    out.append(_footer())
    return "".join(out)


def _axis_legend(label="外轮廓 / 孔洞") -> str:
    return (f'<g font-family="sans-serif" font-size="12" fill="{_COLORS["ink"]}">'
            '<path d="M30 455 L84 455 M30 455 L30 405" stroke="#26384a" stroke-width="2"/>'
            '<path d="M84 455 l-7 -4 v8 z M30 405 l-4 7 h8 z" fill="#26384a"/>'
            '<text x="88" y="459">+X</text><text x="24" y="397">+Y</text>'
            f'<text x="25" y="482">{_label(label)}</text></g>')


def _source_points(profile: Mapping[str, Any]) -> tuple[list[tuple[float, float]], float | None]:
    source = validate_source_profile(dict(profile))
    kind = source["kind"]
    if kind == "point":
        return [], 2.0
    if kind == "disk":
        return [], source["radius_mm"]
    if kind == "annulus":
        return [], source["outer_radius_mm"]
    angle = math.radians(source.get("angle_deg", 0.0))
    c, s = math.cos(angle), math.sin(angle)
    if kind == "line":
        half = source["length_mm"] / 2
        return [(-half*c, -half*s), (half*c, half*s)], None
    a, b = source["length_mm"] / 2, source["width_mm"] / 2
    return [(x*c-y*s, x*s+y*c) for x, y in [(-a,-b),(a,-b),(a,b),(-a,b)]], None


def _source_svg(profile: Mapping[str, Any], title="蒸发源几何") -> str:
    source = validate_source_profile(dict(profile))
    points, radius = _source_points(source)
    if points:
        box = _extent(points)
    else:
        r = radius or 1
        box = (-r, -r, r, r)
        if source["kind"] == "annulus":
            r = source["inner_radius_mm"]
    out = _header(title, f"局部 XY / mm · {source['kind']} · 角指数 n = {_fmt(source['source_exponent'])}")
    if source["kind"] == "line":
        a, b = _xy_svg(points, box, 800, 560)
        out.append(f'<line x1="{_fmt(a[0])}" y1="{_fmt(a[1])}" x2="{_fmt(b[0])}" y2="{_fmt(b[1])}" stroke="{_COLORS["orange"]}" stroke-width="8" stroke-linecap="round"/>')
    elif source["kind"] == "rectangle":
        out.append(_poly(points, box, 800, 560, color=_COLORS["orange"], fill="rgba(225,138,68,.22)"))
    elif source["kind"] == "point":
        center = _xy_svg([(0, 0)], box, 800, 560)[0]
        out.append(f'<circle cx="{_fmt(center[0])}" cy="{_fmt(center[1])}" r="8" fill="{_COLORS["orange"]}"/>')
    else:
        out.append(_circle(0, 0, source["outer_radius_mm"] if source["kind"] == "annulus" else radius or 1,
                           box, 800, 560, color=_COLORS["orange"], fill="rgba(225,138,68,.22)"))
        if source["kind"] == "annulus" and source["inner_radius_mm"] > 0:
            out.append(_circle(0, 0, source["inner_radius_mm"], box, 800, 560,
                               color=_COLORS["orange"], fill="#fff", stroke_width=1.5))
    out.extend([_axis_legend("源几何 · 局部 XY"), _footer()])
    return "".join(out)


def _substrate_svg(profile: Mapping[str, Any], surface_points: list[list[float]]) -> str:
    """Draw the validated outline and existing X-diameter surface samples."""
    sub = validate_substrate_profile(dict(profile))
    outline = sub["outline"]
    if outline["kind"] == "circle":
        radius = outline["radius_mm"]
        box = (-radius, -radius, radius, radius)
    else:
        points = [tuple(p) for p in outline_polygon_points(outline)]
        box = _extent(points)
    out = _header("基片轮廓与面型", f"局部 XY / mm · {outline['kind']} · surface = {sub['surface']['kind']}")
    xy_rect = (45.0, 95.0, 420.0, 445.0)
    if outline["kind"] == "circle":
        out.append(_circle(0, 0, radius, box, 800, 560, color=_COLORS["green"],
                           fill="rgba(61,152,116,.14)", plot_rect=xy_rect))
    else:
        out.append(_poly(points, box, 800, 560, color=_COLORS["green"],
                         fill="rgba(61,152,116,.14)", plot_rect=xy_rect))
    out.append('<text x="75" y="472" font-family="sans-serif" font-size="12" fill="#26384a">俯视轮廓 · 局部 XY / mm</text>')
    out.append(_profile_panel(surface_points))
    out.append(_footer())
    return "".join(out)


def _profile_panel(samples: list[list[float]]) -> str:
    points = [(float(x), float(z)) for x, z in samples]
    xmin, zmin = min(x for x, _ in points), min(z for _, z in points)
    xmax, zmax = max(x for x, _ in points), max(z for _, z in points)
    if xmax <= xmin:
        raise ValueError("面型剖面X范围无效")
    if zmax - zmin <= 1e-12:
        zmin, zmax = zmin - 0.5, zmax + 0.5
    else:
        pad = (zmax - zmin) * 0.08
        zmin, zmax = zmin - pad, zmax + pad
    left, top, right, bottom = 490.0, 135.0, 765.0, 430.0
    mapped = [(left + (x - xmin) / (xmax - xmin) * (right-left),
               bottom - (z - zmin) / (zmax - zmin) * (bottom-top))
              for x, z in points]
    coords = " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in mapped)
    zero_y = bottom - (0.0 - zmin) / (zmax - zmin) * (bottom-top)
    return (f'<g font-family="sans-serif" fill="{_COLORS["ink"]}"><rect x="470" y="95" width="320" height="375" rx="8" fill="#f7fafc" stroke="#d9e2ec"/>'
            '<text x="490" y="120" font-size="14" font-weight="700">X 直径面型剖面</text>'
            f'<rect x="{_fmt(left)}" y="{_fmt(top)}" width="{_fmt(right-left)}" height="{_fmt(bottom-top)}" fill="#fff" stroke="#aebdcb"/>'
            f'<line x1="{_fmt(left)}" y1="{_fmt(zero_y)}" x2="{_fmt(right)}" y2="{_fmt(zero_y)}" stroke="#718096" stroke-dasharray="3 3"/>'
            f'<polyline points="{coords}" fill="none" stroke="{_COLORS["green"]}" stroke-width="2.5"/>'
            f'<text x="{_fmt(left)}" y="{_fmt(bottom+18)}" font-size="11">X = {_fmt(xmin)} mm</text>'
            f'<text x="{_fmt(right)}" y="{_fmt(bottom+18)}" text-anchor="end" font-size="11">X = {_fmt(xmax)} mm</text>'
            f'<text x="{_fmt(left)}" y="{_fmt(top-8)}" font-size="11">Z相对中心 / mm</text>'
            f'<text x="{_fmt(right-4)}" y="{_fmt(top+14)}" text-anchor="end" font-size="10">{_fmt(zmax)} mm</text>'
            f'<text x="{_fmt(right-4)}" y="{_fmt(bottom-5)}" text-anchor="end" font-size="10">{_fmt(zmin)} mm</text>'
            '<text x="490" y="455" font-size="11" fill="#718096">纵轴自动缩放以便观察曲面弧度</text></g>')


def _machine_svg(preview: Mapping[str, Any], substrate_profile=None) -> str:
    machine = preview["data"]
    sources = preview.get("sources", machine.get("sources", []))
    masks = preview.get("masks", machine.get("masks", []))
    carrier = (_f(machine["carrier_x_mm"]), _f(machine["carrier_y_mm"]))
    orbit = _f(machine["planet_offset_mm"])
    sun_radius = _f(machine["gear_module_mm"]) * int(machine["sun_teeth"]) / 2
    planet_radius = _f(machine["gear_module_mm"]) * int(machine["planet_teeth"]) / 2
    substrate_center = (carrier[0] + orbit, carrier[1])
    tilt_deg = _f(machine["planet_tilt_deg"])
    tilt = math.radians(tilt_deg)
    substrate_points = []
    substrate_name = "未提供基片文件"
    if substrate_profile is not None:
        substrate_profile = validate_substrate_profile(dict(substrate_profile))
        outline = substrate_profile["outline"]
        substrate_name = substrate_profile["name"]
        if outline["kind"] == "circle":
            radius = outline["radius_mm"]
            local_outline = [(radius * math.cos(2*math.pi*i/120),
                              radius * math.sin(2*math.pi*i/120)) for i in range(120)]
        else:
            local_outline = [tuple(p) for p in outline_polygon_points(outline)]
        import numpy as np
        from .substrate_surface import surface_height_and_normal

        local = np.asarray(local_outline, dtype=float)
        heights = surface_height_and_normal(local[:, 0], local[:, 1],
            effective_radius_mm(substrate_profile), substrate_profile["surface"])[0]
        cos_tilt, sin_tilt = math.cos(tilt), math.sin(tilt)
        for (lx, ly), height in zip(local_outline, heights):
            height = _f(height)
            # Existing _planet_surface_pose at the zero-phase inward radial direction.
            projected_x = lx*cos_tilt + height*sin_tilt
            substrate_points.append((substrate_center[0] + projected_x,
                                     substrate_center[1] + ly))
    points = [carrier, substrate_center, (carrier[0]-sun_radius, carrier[1]-sun_radius),
              (carrier[0]+sun_radius, carrier[1]+sun_radius),
              (substrate_center[0]-planet_radius, substrate_center[1]-planet_radius),
              (substrate_center[0]+planet_radius, substrate_center[1]+planet_radius),
              *substrate_points]
    for source in sources:
        center = (_f(source["x_mm"]), _f(source["y_mm"]))
        points.append(center)
        profile = source.get("profile")
        if profile:
            local, radius = _source_points(profile)
            if local:
                points.extend((center[0] + x, center[1] + y) for x, y in local)
            elif radius:
                points.extend(((center[0]-radius, center[1]-radius),
                               (center[0]+radius, center[1]+radius)))
    for board in masks:
        profile = board.get("profile")
        if profile:
            points.extend(tuple(p) for p in profile["outer"])
    box = _extent(points, 500)
    span = max(box[2]-box[0], box[3]-box[1], 500)
    margin = span * .15
    box = box[0]-margin, box[1]-margin, box[2]+margin, box[3]+margin
    out = _header("机器结构俯视示意", "机架 XY / mm · 行星盘零相位投影；盘倾角为示意投影", 900, 620)
    w, h = 900, 620
    plot_rect = _plot_rect(w, h)
    out.append(_circle(carrier[0], carrier[1], orbit, box, w, h, color=_COLORS["muted"], dash="5 5", stroke_width=1.3, plot_rect=plot_rect))
    out.append(_circle(carrier[0], carrier[1], sun_radius, box, w, h, color=_COLORS["blue"], stroke_width=2.0, plot_rect=plot_rect))
    out.append(_circle(substrate_center[0], substrate_center[1], planet_radius, box, w, h, color=_COLORS["green"], stroke_width=1.3, dash="4 3", plot_rect=plot_rect))
    if substrate_points:
        out.append(_poly(substrate_points, box, w, h, color=_COLORS["green"],
                         fill="rgba(61,152,116,.14)", stroke_width=2.2, plot_rect=plot_rect))
        if abs(tilt_deg) > 1e-12:
            center_xy = _xy_svg([substrate_center], box, w, h, plot_rect)[0]
            arrow_y = center_xy[1] - 15
            out.append(f'<path d="M{_fmt(center_xy[0]+48)} {_fmt(arrow_y)} H{_fmt(center_xy[0]+7)} l8 -5 m-8 5 l8 5" fill="none" stroke="#8a5aa5" stroke-width="2"/><text x="{_fmt(center_xy[0]+10)}" y="{_fmt(arrow_y-7)}" font-family="sans-serif" font-size="10" fill="#8a5aa5">向载架</text>')
    else:
        xy0 = _xy_svg([substrate_center], box, w, h, plot_rect)[0]
        out.append(f'<circle cx="{_fmt(xy0[0])}" cy="{_fmt(xy0[1])}" r="4" fill="{_COLORS["green"]}"/>')
    cxy = _xy_svg([carrier], box, w, h, plot_rect)[0]
    out.append(f'<circle cx="{_fmt(cxy[0])}" cy="{_fmt(cxy[1])}" r="4" fill="{_COLORS["blue"]}"/><text x="{_fmt(cxy[0]+7)}" y="{_fmt(cxy[1]-7)}" font-family="sans-serif" font-size="12">载架 / 太阳轮参考</text>')
    for i, entry in enumerate(sources, 1):
        x, y = _f(entry["x_mm"]), _f(entry["y_mm"])
        xy = _xy_svg([(x, y)], box, w, h, plot_rect)[0]
        profile = entry.get("profile")
        if profile:
            local, radius = _source_points(profile)
            if profile["kind"] == "line":
                endpoints = _xy_svg([(x + px, y + py) for px, py in local], box, w, h, plot_rect)
                out.append(f'<line x1="{_fmt(endpoints[0][0])}" y1="{_fmt(endpoints[0][1])}" x2="{_fmt(endpoints[1][0])}" y2="{_fmt(endpoints[1][1])}" stroke="{_COLORS["orange"]}" stroke-width="5" stroke-linecap="round"/>')
            elif profile["kind"] == "rectangle":
                out.append(_poly([(x + px, y + py) for px, py in local], box, w, h,
                                 color=_COLORS["orange"], fill="rgba(225,138,68,.28)", stroke_width=1.6, plot_rect=plot_rect))
            elif profile["kind"] == "point":
                out.append(f'<circle cx="{_fmt(xy[0])}" cy="{_fmt(xy[1])}" r="5" fill="{_COLORS["orange"]}"/>')
            elif radius:
                out.append(_circle(x, y, radius, box, w, h, color=_COLORS["orange"],
                                   fill="rgba(225,138,68,.28)", stroke_width=1.6, plot_rect=plot_rect))
                if profile["kind"] == "annulus" and profile["inner_radius_mm"] > 0:
                    out.append(_circle(x, y, profile["inner_radius_mm"], box, w, h,
                                       color=_COLORS["orange"], fill="#fff", stroke_width=1.2, plot_rect=plot_rect))
            else:
                out.append(f'<circle cx="{_fmt(xy[0])}" cy="{_fmt(xy[1])}" r="5" fill="{_COLORS["orange"]}"/>')
        else:
            out.append(f'<circle cx="{_fmt(xy[0])}" cy="{_fmt(xy[1])}" r="5" fill="{_COLORS["orange"]}"/>')
        out.append(f'<text x="{_fmt(xy[0]+8)}" y="{_fmt(xy[1]-8)}" font-family="sans-serif" font-size="12">源{i}</text>')
    for i, entry in enumerate(masks, 1):
        profile = entry.get("profile")
        if profile:
            # Machine view uses the exact installed outline in machine coordinates.
            paths = []
            for ring in [profile["outer"], *profile.get("holes", [])]:
                mapped = _xy_svg([tuple(p) for p in ring], box, w, h, plot_rect)
                paths.append("M" + " L".join(f"{_fmt(x)},{_fmt(y)}" for x, y in mapped) + " Z")
            out.append(f'<path d="{" ".join(paths)}" fill="rgba(103,184,199,.12)" '
                       'fill-rule="evenodd" stroke="#67b8c7" stroke-width="1.5" stroke-dasharray="3 3"/>')
    out.append(f'<g font-family="sans-serif" font-size="12" fill="{_COLORS["ink"]}"><text x="655" y="110">公转半径 R={_fmt(orbit)} mm</text><text x="655" y="135">太阳轮节圆 R={_fmt(sun_radius)} mm</text><text x="655" y="160">行星轮节圆 R={_fmt(planet_radius)} mm</text><text x="655" y="195">盘倾角：{_fmt(tilt_deg)}°</text><text x="655" y="218">正角度向载架中心侧抬高</text><text x="655" y="255">工件盘：{_label(substrate_name)}</text><text x="655" y="278">边界：{'按基片轮廓绘制' if substrate_points else '机器文件未含基片边界'}</text><text x="655" y="310">橙色：源　青色：修正板</text><text x="655" y="342">虚线圆：齿轮参考节圆</text><text x="655" y="365">未绘制齿形或机械厚度</text><text x="655" y="405">投影示意：行星盘中心零相位</text><text x="655" y="428">基片轮廓按盘倾角投影</text><text x="655" y="470">源/基片 Z 位于侧视信息</text><text x="655" y="493">机器俯视图不表示三维装配</text></g>')
    source_z = [_f(e["z_mm"]) for e in sources]
    mask_z = [_f(e["profile"]["z_mm"]) for e in masks if e.get("profile")]
    ztext = f"源Z {min(source_z):.4g}–{max(source_z):.4g}" if source_z else "无源Z"
    if mask_z:
        ztext += f" · 板Z {min(mask_z):.4g}–{max(mask_z):.4g}"
    ztext += f" · 基片中心Z {_fmt(_f(machine['substrate_z_mm']))} mm"
    out.append(f'<text x="655" y="525" font-family="sans-serif" font-size="10" fill="#718096">高度 / mm：{_label(ztext)}</text>')
    out.append(_footer(610))
    return "".join(out)


def render_design_visualization(service, kind: str,
                                data: Mapping[str, Any] | str | os.PathLike[str], *,
                                path: str | os.PathLike[str] | None = None,
                                substrate_profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Write a deterministic offline SVG for source, mask, substrate or machine.

    ``data`` and ``path`` follow ``CoatService.preview``: paths are resolved and
    checked by the service, while inline mappings are validated there.
    """
    if kind not in {"source", "mask", "substrate", "machine"}:
        raise ValueError("几何图仅支持 source、mask、substrate、machine")
    preview = service.preview(kind, data, path=path)
    if preview.get("issues"):
        raise ValueError("设计存在校验问题，不能生成几何图：" + "；".join(map(str, preview["issues"])))
    profile = preview["data"]
    if kind == "source":
        svg = _source_svg(profile)
    elif kind == "mask":
        svg = _mask_svg(profile)
    elif kind == "substrate":
        samples = service.surface_profile(profile["surface"], effective_radius_mm(profile))
        svg = _substrate_svg(profile, samples)
    else:
        normalized_substrate = (validate_substrate_profile(dict(substrate_profile))
                                if substrate_profile is not None else None)
        svg = _machine_svg(preview, normalized_substrate)
    artifact_data = ({"machine": profile, "substrate": normalized_substrate}
                     if kind == "machine" else profile)
    target = _artifact_path(kind, artifact_data, "svg")
    _atomic_write(target, svg)
    info = file_info(target)
    return {"files": [info], "summary": {"kind": kind, "name": profile.get("name", kind),
            "coordinate_system": "machine XY" if kind in {"mask", "machine"} else "local XY",
            "units": "mm", "offline": True}}


def _process_detail_html(process: Mapping[str, Any]) -> str:
    rows = []
    for index, step in enumerate(process.get("steps", []), 1):
        slots = step.get("mask_slots")
        mask_text = "沿用机器启用" if slots is None else ("禁用" if slots == [] else ", ".join(str(int(s)+1) for s in slots))
        values = [index, step.get("name", f"膜层 {index}"), step.get("source_slot", ""),
                  step.get("target_nm", ""), step.get("rate_nm_s", ""),
                  step.get("target_mode", ""), step.get("monitor_slot") or "—", mask_text]
        rows.append("<tr>" + "".join(f"<td>{_label(v)}</td>" for v in values) + "</tr>")
    columns = ["层序", "名称", "源位次", "目标厚度 / nm", "速率 / nm·s⁻¹", "厚度基准", "监测器位次", "修正板"]
    count = len(rows)
    return """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>工艺详情</title><style>
body{font:15px system-ui,sans-serif;color:#26384a;background:#f7fafc;margin:0;padding:24px}main{max-width:1200px;margin:auto;background:white;padding:24px;border-radius:12px;box-shadow:0 2px 14px #26384a18}h1{margin:0 0 6px}p{color:#718096}input{font:inherit;padding:9px 12px;border:1px solid #cbd5e0;border-radius:7px;width:min(420px,90%)}.bar{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin:18px 0}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #e2e8f0;white-space:nowrap}th{position:sticky;top:0;background:#edf4f8;cursor:pointer}tbody tr:hover{background:#f7fafc}.scroll{overflow:auto;max-height:75vh}.empty{padding:20px;color:#718096}
</style><main><h1>""" + _label(process.get("name", "工艺详情")) + f"""</h1><p>共 {count} 层 · 离线查看 · 源和监测器位次从 1 开始，修正板显示为 1 起始名称。</p><div class="bar"><label>筛选层名或参数 <input id="q" type="search" placeholder="输入关键词"></label><span id="shown">显示 {count} / {count} 层</span></div><div class="scroll"><table><thead><tr>""" + "".join(f"<th>{_label(c)}</th>" for c in columns) + """</tr></thead><tbody id="rows">""" + "".join(rows) + """</tbody></table></div><script>
const q=document.getElementById('q'), rows=[...document.querySelectorAll('#rows tr')], shown=document.getElementById('shown');q.addEventListener('input',()=>{const term=q.value.trim().toLocaleLowerCase();let n=0;for(const row of rows){const visible=row.textContent.toLocaleLowerCase().includes(term);row.hidden=!visible;if(visible)n++;}shown.textContent=`显示 ${n} / ${rows.length} 层`;});document.querySelectorAll('th').forEach((th,i)=>th.addEventListener('click',()=>{const body=document.getElementById('rows'),ascending=th.dataset.order!=='asc';th.dataset.order=ascending?'asc':'desc';rows.sort((a,b)=>{const x=a.cells[i].textContent,y=b.cells[i].textContent,nx=Number(x),ny=Number(y),numeric=x!==''&&y!==''&&Number.isFinite(nx)&&Number.isFinite(ny);const cmp=numeric?nx-ny:x.localeCompare(y,'zh-CN',{numeric:true});return ascending?cmp:-cmp;});rows.forEach(row=>body.appendChild(row));q.dispatchEvent(new Event('input'));}));
</script></main></html>"""


def render_process_detail_html(service, data: Mapping[str, Any] | str | os.PathLike[str], *,
                               path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Write an offline searchable/sortable process table without returning layers."""
    preview = service.preview("process", data, path=path)
    if preview.get("issues"):
        raise ValueError("工艺存在校验问题，不能生成详情页：" + "；".join(map(str, preview["issues"])))
    process = preview["data"]
    target = _artifact_path("process-detail", process, "html")
    _atomic_write(target, _process_detail_html(process))
    return {"files": [file_info(target)], "summary": {
        "kind": "process-detail", "name": process.get("name", "工艺详情"),
        "layer_count": len(process.get("steps", [])), "offline": True,
        "includes_full_layer_table": True}}
