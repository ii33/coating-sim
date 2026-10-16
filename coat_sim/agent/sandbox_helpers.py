"""Small user-facing library copied into each isolated script workspace.

Use ``from coat_plot import chart, save_chart, read_csv`` inside a script.
Pygal output is offline SVG: no scripts, remote styles, fonts, or images.
"""
from __future__ import annotations
import csv
import json
import math
from html import escape
from pathlib import Path
import pygal
from pygal.style import Style


_CHARTS = {
    "line": pygal.Line, "bar": pygal.Bar, "horizontal_bar": pygal.HorizontalBar,
    "xy": pygal.XY, "pie": pygal.Pie, "radar": pygal.Radar,
    "box": pygal.Box, "histogram": pygal.Histogram,
}


def chart(kind="line", title="", **options):
    """Construct a lightweight offline chart with readable local text."""
    if kind not in _CHARTS:
        raise ValueError("支持的图表：" + ", ".join(_CHARTS))
    defaults = {
        "title": title, "js": [], "width": 900, "height": 560,
        "explicit_size": True, "show_legend": True, "legend_at_bottom": True,
        "style": Style(background="#ffffff", plot_background="#ffffff",
                       foreground="#202633", foreground_strong="#101520",
                       foreground_subtle="#777e8b", font_family="sans-serif",
                       colors=("#2788dd", "#f28b35", "#25a888", "#a46bd1", "#dc5e70")),
    }
    defaults.update(options)
    defaults["js"] = []
    return _CHARTS[kind](**defaults)


def save_chart(value, filename="chart.svg"):
    """Write under ./outputs. The host independently checks and sanitizes SVG."""
    path = _output_path(filename)
    if path.suffix.lower() != ".svg":
        raise ValueError("图表使用 .svg 扩展名")
    if not isinstance(value, SVGChart):
        value.js = []
    value.render_to_file(str(path))
    print("图表已生成：" + path.name)
    return path


def _output_path(filename):
    root = (Path.cwd() / "outputs").resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("输出必须位于 outputs 内")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def save_table(rows, filename="table.csv", columns=None):
    rows = list(rows)
    if columns is None:
        columns = list(rows[0]) if rows and isinstance(rows[0], dict) else []
    path = _output_path(filename)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def read_csv(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


class SVGChart:
    """Passive, offline scientific SVG; same save_chart contract as Pygal."""
    def __init__(self, svg):
        self.svg = svg

    def render(self):
        return self.svg.encode("utf-8")

    def render_to_file(self, filename):
        Path(filename).write_text(self.svg, encoding="utf-8")


def capabilities():
    """Explicit supported APIs. This module needs only stdlib and existing Pygal."""
    return {
        "chart": list(_CHARTS),
        "scientific": ["heatmap", "contour", "radial_profile", "profile_chart"],
        "grid_contract": "x[N], y[M], values[M][N]; values[row_y][column_x], ascending or descending axes, None/nonfinite masked",
        "exports": ["offline SVG", "CSV"], "max_grid_cells": 65536,
        "svg_limits": {"bytes": 8 * 1024 * 1024, "elements": 100000},
        "examples": ["save_chart(heatmap(x,y,values), 'heatmap.svg')",
                     "save_chart(contour(x,y,values,levels=8), 'contours.svg')",
                     "rows=radial_profile(x,y,values,bin_width=5); save_chart(profile_chart(rows), 'radial.svg')"],
        "limitations": "2D passive SVG, no interactive browser/3D; radial mean is equal sample mean, not area weighted for nonuniform grids",
    }


def _number(value):
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _axis(values, name):
    result = [_number(v) for v in values]
    if len(result) < 2 or any(v is None for v in result):
        raise ValueError(name + " 须至少有两个有限坐标")
    differences = [b-a for a, b in zip(result, result[1:])]
    if not (all(v > 0 for v in differences) or all(v < 0 for v in differences)):
        raise ValueError(name + " 须严格单调，无重复坐标")
    return result


def _grid(x, y, values, mask=None):
    x, y = _axis(x, "x"), _axis(y, "y")
    rows = [list(row) for row in values]
    if len(rows) != len(y) or any(len(row) != len(x) for row in rows):
        raise ValueError("values 形状须为 [len(y)][len(x)]，不自动转置")
    if len(x)*len(y) > 65536:
        raise ValueError("图表网格超过65536点，请明确选择降采样后再绘图；工具不自动丢数据")
    if mask is not None:
        mask = [list(row) for row in mask]
        if len(mask) != len(y) or any(len(row) != len(x) for row in mask):
            raise ValueError("mask 形状须与 values 一致，True 表示有效")
    rows = [[_number(v) if mask is None or mask[j][i] else None
             for i, v in enumerate(row)] for j, row in enumerate(rows)]
    finite = [v for row in rows for v in row if v is not None]
    if not finite:
        raise ValueError("网格没有有效有限数值")
    return x, y, rows, min(finite), max(finite)


_PALETTE = ((35, 38, 125), (28, 139, 195), (39, 196, 149), (244, 221, 66), (194, 43, 36))


def _color(value, low, high):
    position = (0.5 if high == low else max(0, min(1, (value-low)/(high-low))))*(len(_PALETTE)-1)
    index = min(len(_PALETTE)-2, int(position))
    fraction = position-index
    return "#" + "".join(f"{round(a+(b-a)*fraction):02x}"
                          for a, b in zip(_PALETTE[index], _PALETTE[index+1]))


def _limits(low, high, vmin, vmax):
    lower = low if vmin is None else _number(vmin)
    upper = high if vmax is None else _number(vmax)
    if lower is None or upper is None or lower > upper:
        raise ValueError("色阶范围须有限，且 vmin <= vmax")
    if lower == upper:
        if vmin is not None or vmax is not None:
            raise ValueError("手动色阶须 vmin < vmax")
        padding = max(abs(lower)*0.001, 0.001)
        return lower-padding, upper+padding
    return lower, upper


def _edges(axis):
    return [axis[0]-(axis[1]-axis[0])/2,
            *((a+b)/2 for a, b in zip(axis, axis[1:])),
            axis[-1]+(axis[-1]-axis[-2])/2]


def _svg_frame(xmin, xmax, ymin, ymax, title, x_label, y_label, width, height, equal_aspect):
    if not xmin < xmax or not ymin < ymax:
        raise ValueError("绘图坐标范围须有正跨度")
    if not 420 <= width <= 2400 or not 320 <= height <= 1800:
        raise ValueError("图尺寸须 width=420..2400、height=320..1800")
    left, top, plot_width, plot_height = 85.0, 62.0, width-225.0, height-145.0
    if equal_aspect:
        scale = min(plot_width/(xmax-xmin), plot_height/(ymax-ymin))
        fitted_width, fitted_height = scale*(xmax-xmin), scale*(ymax-ymin)
        left += (plot_width-fitted_width)/2
        top += (plot_height-fitted_height)/2
        plot_width, plot_height = fitted_width, fitted_height
    px = lambda value: left+(value-xmin)/(xmax-xmin)*plot_width
    py = lambda value: top+plot_height-(value-ymin)/(ymax-ymin)*plot_height
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="#ffffff"/>',
             '<g font-family="sans-serif" font-size="14" fill="#263340">',
             f'<text x="{width/2}" y="30" text-anchor="middle" font-size="20">{escape(str(title))}</text>',
             f'<rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" fill="#f3f4f6"/>']
    footer = [f'<rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" fill="none" stroke="#536171"/>']
    for index in range(6):
        xv, yv = xmin+(xmax-xmin)*index/5, ymin+(ymax-ymin)*index/5
        footer.extend([f'<line x1="{px(xv)}" y1="{py(ymin)}" x2="{px(xv)}" y2="{py(ymin)+6}" stroke="#536171"/>',
                       f'<text x="{px(xv)}" y="{py(ymin)+24}" text-anchor="middle">{xv:.5g}</text>',
                       f'<line x1="{left-6}" y1="{py(yv)}" x2="{left}" y2="{py(yv)}" stroke="#536171"/>',
                       f'<text x="{left-10}" y="{py(yv)+5}" text-anchor="end">{yv:.5g}</text>'])
    footer += [f'<text x="{left+plot_width/2}" y="{height-20}" text-anchor="middle">{escape(str(x_label))}</text>',
               f'<text x="20" y="{top+plot_height/2}" text-anchor="middle" transform="rotate(-90 20 {top+plot_height/2})">{escape(str(y_label))}</text>']
    return parts, footer, px, py, (left, top, plot_width, plot_height)


def _legend(parts, frame, low, high, value_label):
    left, top, plot_width, plot_height = frame
    xpos = left+plot_width+28
    for index in range(128):
        value = high-(high-low)*(index+0.5)/128
        parts.append(f'<rect x="{xpos}" y="{top+plot_height*index/128}" width="18" height="{plot_height/128+0.1}" fill="{_color(value,low,high)}"/>')
    for index in range(6):
        parts.append(f'<text x="{xpos+26}" y="{top+plot_height*index/5+5}">{high-(high-low)*index/5:.6g}</text>')
    parts.append(f'<text x="{xpos}" y="{top+plot_height+40}">{escape(str(value_label))}</text>')


def heatmap(x, y, values, title="膜厚热力图", *, x_label="X / mm", y_label="Y / mm",
            value_label="膜厚 / nm", vmin=None, vmax=None, equal_aspect=True,
            mask=None, width=900, height=650):
    """Pixel-centered 2D map; no interpolation, finite masks remain blank."""
    x, y, rows, low, high = _grid(x, y, values, mask)
    low, high = _limits(low, high, vmin, vmax)
    xe, ye = _edges(x), _edges(y)
    parts, footer, px, py, frame = _svg_frame(min(xe), max(xe), min(ye), max(ye), title,
                                           x_label, y_label, width, height, equal_aspect)
    for j, row in enumerate(rows):
        for i, value in enumerate(row):
            if value is not None:
                x1, x2 = sorted((px(xe[i]), px(xe[i+1])))
                y1, y2 = sorted((py(ye[j]), py(ye[j+1])))
                parts.append(f'<rect x="{x1:.3f}" y="{y1:.3f}" width="{x2-x1:.3f}" height="{y2-y1:.3f}" fill="{_color(value,low,high)}"/>')
    parts.extend(footer)
    _legend(parts, frame, low, high, value_label)
    return SVGChart("\n".join(parts)+"</g></svg>")


def contour(x, y, values, title="膜厚等高线", *, levels=8, x_label="X / mm", y_label="Y / mm",
            value_label="膜厚 / nm", vmin=None, vmax=None, equal_aspect=True,
            mask=None, width=900, height=650):
    """Marching squares, linear edge interpolation, arithmetic-center saddle rule."""
    x, y, rows, actual_low, actual_high = _grid(x, y, values, mask)
    low, high = _limits(actual_low, actual_high, vmin, vmax)
    if isinstance(levels, int):
        if not 1 <= levels <= 32:
            raise ValueError("等高线数量须为1..32")
        levels = [low+(high-low)*(i+1)/(levels+1) for i in range(levels)]
    else:
        levels = [_number(v) for v in levels]
        if not levels or len(levels) > 32 or any(v is None for v in levels):
            raise ValueError("levels 须为1..32个有限值")
        levels = sorted(set(levels))
    parts, footer, px, py, frame = _svg_frame(min(x), max(x), min(y), max(y), title,
                                           x_label, y_label, width, height, equal_aspect)
    segment_count = 0
    for level in levels:
        segments = []
        for j in range(len(y)-1):
            for i in range(len(x)-1):
                v = (rows[j][i], rows[j][i+1], rows[j+1][i+1], rows[j+1][i])
                if any(a is None for a in v) or not min(v) <= level <= max(v):
                    continue
                positions = ((x[i],y[j]),(x[i+1],y[j]),(x[i+1],y[j+1]),(x[i],y[j+1]))
                crossings = {}
                for edge in range(4):
                    following = (edge+1)%4
                    if (v[edge] >= level) != (v[following] >= level):
                        fraction = (level-v[edge])/(v[following]-v[edge])
                        a, b = positions[edge], positions[following]
                        crossings[edge] = (a[0]+fraction*(b[0]-a[0]), a[1]+fraction*(b[1]-a[1]))
                if len(crossings) == 2:
                    pairs = [tuple(crossings)]
                elif len(crossings) == 4:
                    center_high = sum(v)/4 >= level
                    pairs = [((corner-1)%4, corner) for corner in range(4)
                             if (v[corner] >= level) != center_high]
                else:
                    continue
                for a, b in pairs:
                    start, end = crossings[a], crossings[b]
                    if start != end:
                        segment_count += 1
                        if segment_count > 100000:
                            raise ValueError("等高线超过100000线段，请明确减少层数或降采样")
                        segments.append(f'M{px(start[0]):.3f},{py(start[1]):.3f}L{px(end[0]):.3f},{py(end[1]):.3f}')
        if segments:
            parts.append(f'<path d="{" ".join(segments)}" fill="none" stroke="{_color(level,low,high)}" stroke-width="1.5"><title>{escape(str(value_label))}: {level:.8g}</title></path>')
    parts.extend(footer)
    _legend(parts, frame, low, high, value_label)
    return SVGChart("\n".join(parts)+"</g></svg>")


def radial_profile(x, y, values, *, center=(0, 0), bin_width=None, bins=30, radius=None, mask=None):
    """Equal-sample ring min/mean/max/count; returns plain rows for CSV/chart."""
    x, y, rows, _, _ = _grid(x, y, values, mask)
    cx, cy = (_number(v) for v in center)
    if cx is None or cy is None:
        raise ValueError("圆心须为有限坐标")
    maximum = max(math.hypot(a-cx,b-cy) for a in x for b in y) if radius is None else _number(radius)
    if maximum is None or maximum <= 0:
        raise ValueError("半径须大于0")
    if bin_width is not None:
        step = _number(bin_width)
        if step is None or step <= 0:
            raise ValueError("分桶宽度须大于0")
        count = math.ceil(maximum/step)
    else:
        if not isinstance(bins,int) or not 1 <= bins <= 2000:
            raise ValueError("bins须为1..2000整数")
        count, step = bins, maximum/bins
    if count > 2000:
        raise ValueError("径向分桶超过2000，请增大桶宽")
    groups = [[] for _ in range(count)]
    for j, row in enumerate(rows):
        for i, value in enumerate(row):
            distance = math.hypot(x[i]-cx,y[j]-cy)
            if value is not None and distance <= maximum:
                groups[min(count-1,int(distance/step))].append(value)
    return [{"radius": (index*step+min(maximum,(index+1)*step))/2,
             "radius_min": index*step, "radius_max": min(maximum,(index+1)*step),
             "min": min(group) if group else None, "mean": math.fsum(group)/len(group) if group else None,
             "max": max(group) if group else None, "count": len(group)}
            for index, group in enumerate(groups)]


def profile_chart(rows, title="径向膜厚分布", *, x_label="半径 / mm", y_label="膜厚 / nm", width=900, height=560):
    """Mean line with min/max band; empty rings break the curve, not interpolated."""
    rows = list(rows)
    for row in rows:
        if not isinstance(row, dict) or not all(key in row for key in ("radius", "radius_min", "radius_max", "min", "mean", "max")):
            raise ValueError("径向行须包含radius/radius_min/radius_max/min/mean/max；建议直接传radial_profile结果")
        bounds = [_number(row[key]) for key in ("radius_min", "radius", "radius_max")]
        if any(v is None for v in bounds) or not bounds[0] <= bounds[1] <= bounds[2] or bounds[0] == bounds[2]:
            raise ValueError("每个径向桶须有有限正跨度，且radius位于桶范围内")
        values = [_number(row[key]) for key in ("min", "mean", "max")]
        if any(v is None for v in values) and not all(v is None for v in values):
            raise ValueError("空桶的min/mean/max须全为空，有效桶须全为有限值")
        if values[0] is not None and not values[0] <= values[1] <= values[2]:
            raise ValueError("径向桶须min <= mean <= max")
    if any(float(b["radius"]) <= float(a["radius"]) for a,b in zip(rows,rows[1:])):
        raise ValueError("径向桶须按radius严格递增排列")
    finite = [row for row in rows if all(_number(row.get(key)) is not None for key in ("radius","min","mean","max"))]
    if not finite:
        raise ValueError("径向数据没有有效桶")
    xmin, xmax = min(float(r["radius_min"]) for r in finite), max(float(r["radius_max"]) for r in finite)
    ymin, ymax = min(float(r["min"]) for r in finite), max(float(r["max"]) for r in finite)
    padding = max((ymax-ymin)*0.08, abs(ymax)*1e-4, 1e-6)
    parts, footer, px, py, _ = _svg_frame(xmin,xmax,ymin-padding,ymax+padding,title,x_label,y_label,width,height,False)
    groups, current = [], []
    valid_ids = {id(row) for row in finite}
    for row in rows:
        if id(row) in valid_ids:
            current.append(row)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    for group in groups:
        band = [(px(float(r["radius"])),py(float(r["max"]))) for r in group]
        band += [(px(float(r["radius"])),py(float(r["min"]))) for r in reversed(group)]
        parts.append('<polygon points="'+' '.join(f'{a:.3f},{b:.3f}' for a,b in band)+'" fill="#d2e6f9"/>')
        points = [(px(float(r["radius"])),py(float(r["mean"]))) for r in group]
        parts.append('<polyline points="'+' '.join(f'{a:.3f},{b:.3f}' for a,b in points)+'" fill="none" stroke="#267ac0" stroke-width="2"/>')
        for a,b in points:
            parts.append(f'<circle cx="{a:.3f}" cy="{b:.3f}" r="2.5" fill="#267ac0"/>')
    parts.extend(footer)
    parts.append(f'<text x="{width/2}" y="{height-48}" text-anchor="middle">均值曲线 · 阴影为桶内最小/最大值（非置信区间）</text>')
    return SVGChart("\n".join(parts)+"</g></svg>")
