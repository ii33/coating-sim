"""Fixed offline plots from a persisted simulation, without model data transport.

Coordinates are millimetres. Thickness is nm for field_nm, otherwise relative.
Radial bins use finite grid samples with equal weights (project convention).
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import tempfile
import threading
from functools import wraps
from pathlib import Path
from typing import Any

import numpy as np

from coat_sim.core.data_files import data_path, file_info
from coat_sim.core.atomic_io import replace_with_retry


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_atomic(path: Path, content: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", prefix=".plot-", suffix=path.suffix,
                                          dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _radial_rows(x: np.ndarray, y: np.ndarray, field: np.ndarray,
                 center: tuple[float, float], bins: int) -> list[dict[str, Any]]:
    distances = np.hypot(x[None, :] - center[0], y[:, None] - center[1])
    valid = np.isfinite(field)
    maximum = float(distances[valid].max())
    if not math.isfinite(maximum) or maximum <= 0:
        raise ValueError("径向坐标范围须大于0")
    edges = np.linspace(0.0, maximum, bins + 1)
    ids = np.minimum(bins - 1, np.searchsorted(edges, distances[valid], side="right") - 1)
    samples = field[valid]
    count = np.bincount(ids, minlength=bins)
    totals = np.bincount(ids, weights=samples, minlength=bins)
    minima, maxima = np.full(bins, np.inf), np.full(bins, -np.inf)
    np.minimum.at(minima, ids, samples)
    np.maximum.at(maxima, ids, samples)
    return [{"radius_min_mm": float(edges[i]), "radius_mm": float((edges[i] + edges[i + 1]) / 2),
             "radius_max_mm": float(edges[i + 1]), "count": int(count[i]),
             "min": float(minima[i]) if count[i] else None,
             "mean": float(totals[i] / count[i]) if count[i] else None,
             "max": float(maxima[i]) if count[i] else None} for i in range(bins)]


def _duration_rows(result: dict[str, Any], process: dict[str, Any] | None) -> list[dict[str, Any]]:
    steps = result.get("step_results") or []
    if steps:
        return [{"layer": int(row.get("step_index", i)), "source_slot": row.get("source_slot"),
                 "source_name": row.get("source_name", ""), "target_nm": row.get("target_nm"),
                 "rate_nm_s": row.get("rate_nm_s"), "duration_s": row.get("duration_s"),
                 "simulated_duration_s": row.get("simulated_duration_s")}
                for i, row in enumerate(steps, 1)]
    return [{"layer": i, "source_slot": row.get("source_slot"), "source_name": "",
             "target_nm": row.get("target_nm"), "rate_nm_s": row.get("rate_nm_s"),
             "duration_s": float(row["target_nm"]) / float(row["rate_nm_s"]),
             "simulated_duration_s": None}
            for i, row in enumerate((process or {}).get("steps", []), 1)]


_PLOT_LOCK = threading.RLock()


def _serial_plot(function):
    @wraps(function)
    def render(*args, **kwargs):
        # Agg and the local ownership manifest share process state. A second
        # request must see the first request's committed output before reuse.
        with _PLOT_LOCK:
            return function(*args, **kwargs)
    return render


@_serial_plot
def render_result_visualizations(result: dict[str, Any], process: dict[str, Any] | None,
                                 run_id: str, output_directory: str | Path,
                                 charts: list[str] | None = None, *, radial_bins: int = 30,
                                 contour_levels: int = 8,
                                 radial_center_mm: tuple[float, float] = (0.0, 0.0)) -> dict[str, Any]:
    """Render requested heatmap/contour/radial/layer_durations, returning files.

    Identical requests reuse checksum-verified files. A parameter signature gives
    changed chart options a new name. Existing unrelated/edited files are never
    overwritten. Original axes/field are used without implicit downsampling.
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib import font_manager

    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("run_id须为计算返回的32位十六进制标识")
    selected = list(dict.fromkeys(charts if charts is not None else ["heatmap", "contour", "radial", "layer_durations"]))
    allowed = {"heatmap", "contour", "radial", "layer_durations"}
    if not selected or any(name not in allowed for name in selected):
        raise ValueError("charts支持heatmap、contour、radial、layer_durations")
    if isinstance(radial_bins, bool) or not isinstance(radial_bins, int) or not 1 <= radial_bins <= 200:
        raise ValueError("radial_bins须为1..200整数")
    if isinstance(contour_levels, bool) or not isinstance(contour_levels, int) or not 1 <= contour_levels <= 32:
        raise ValueError("contour_levels须为1..32整数")
    center = tuple(float(value) for value in radial_center_mm)
    if len(center) != 2 or not all(math.isfinite(value) for value in center):
        raise ValueError("radial_center_mm须为两个有限毫米坐标")
    directory = data_path(str(output_directory), root_allowed=False)
    directory.mkdir(parents=True, exist_ok=True)
    options = {"run_id": run_id, "radial_bins": radial_bins, "contour_levels": contour_levels,
               "radial_center_mm": center, "implementation_version": 2}
    signature = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()[:12]
    manifest_path = data_path(str(directory / f"visualizations-{run_id}-{signature}.json"), root_allowed=False)
    manifest = {"producer": "coat_sim.result_visualization", "options": options, "outputs": {}}
    if manifest_path.exists():
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        if saved.get("producer") != manifest["producer"] or saved.get("options") != json.loads(json.dumps(options)):
            raise ValueError("图表清单已存在但不属于当前固定可视化请求，未覆盖")
        manifest = saved
    field_name = "field_nm" if "field_nm" in result else "field"
    unit = "nm" if field_name == "field_nm" else "relative"
    x, y, field = np.asarray(result["x"], dtype=float), np.asarray(result["y"], dtype=float), np.asarray(result[field_name], dtype=float)
    if x.ndim != 1 or y.ndim != 1 or field.shape != (len(y), len(x)) or min(len(x), len(y)) < 2:
        raise ValueError("结果场须为values[y][x]并有至少两个轴坐标")
    if not np.isfinite(x).all() or not np.isfinite(y).all() or not np.isfinite(field).any():
        raise ValueError("结果轴或数值场没有有效有限数据")
    if not (np.all(np.diff(x) > 0) or np.all(np.diff(x) < 0)) or not (np.all(np.diff(y) > 0) or np.all(np.diff(y) < 0)):
        raise ValueError("坐标轴须严格单调")
    # Choose an installed font only; no font or other asset is fetched online.
    font = next((name for name in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC")
                 if any(item.name == name for item in font_manager.fontManager.ttflist)), "DejaVu Sans")
    from matplotlib.font_manager import FontProperties
    font_properties = FontProperties(family=font)
    files, notes = [], []
    masked = np.ma.masked_invalid(field)

    def own_target(name: str) -> tuple[Path, bool]:
        target = data_path(str(directory / f"{run_id}-{signature}-{name}"), root_allowed=False)
        recorded = manifest["outputs"].get(name)
        if target.exists():
            if not recorded or recorded.get("sha256") != _digest(target):
                raise ValueError(f"文件已存在且不是可复用的本工具输出，未覆盖：{target.name}")
            return target, True
        return target, False

    def record(name: str, target: Path) -> None:
        manifest["outputs"][name] = {"sha256": _digest(target)}
        _write_atomic(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
        files.append(file_info(target))

    def table(name: str, rows: list[dict[str, Any]]) -> None:
        import io
        target, reused = own_target(name)
        if not reused:
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            _write_atomic(target, stream.getvalue())
        record(name, target)

    for name in selected:
        duration_rows = _duration_rows(result, process) if name == "layer_durations" else []
        if name == "layer_durations" and not duration_rows:
            notes.append("结果不含多层工艺，未生成层时长图。")
            continue
        target, reused = own_target(name + ".png")
        radial_rows = _radial_rows(x, y, field, center, radial_bins) if name == "radial" else []
        if not reused:
            figure = Figure(figsize=(9, 6), dpi=120, layout="constrained")
            FigureCanvasAgg(figure)
            axes = figure.add_subplot(111)
            if name in {"heatmap", "contour"}:
                if name == "heatmap":
                    artist = axes.pcolormesh(x, y, masked, shading="auto", cmap="viridis")
                    axes.set_title("膜厚热图", fontproperties=font_properties)
                else:
                    low, high = float(masked.min()), float(masked.max())
                    if low == high:
                        artist = axes.pcolormesh(x, y, masked, shading="auto", cmap="viridis")
                        notes.append("膜厚场为常量，没有等高线，图中显示恒定数值场。")
                    else:
                        artist = axes.contour(x, y, masked, levels=np.linspace(low, high, contour_levels + 2)[1:-1], cmap="viridis")
                        axes.clabel(artist, inline=True, fontsize=8)
                    axes.set_title("膜厚等高线", fontproperties=font_properties)
                figure.colorbar(artist, ax=axes, label=f"Thickness / {unit}")
                axes.set_xlabel("X / mm")
                axes.set_ylabel("Y / mm")
                axes.set_aspect("equal", adjustable="box")
            elif name == "radial":
                radii = [row["radius_mm"] for row in radial_rows]
                values = lambda key: [np.nan if row[key] is None else row[key] for row in radial_rows]
                axes.plot(radii, values("mean"), label="Mean")
                axes.fill_between(radii, values("min"), values("max"), alpha=.2, label="Min / max")
                axes.set_xlabel("Radius / mm")
                axes.set_ylabel(f"Thickness / {unit}")
                axes.set_title("径向膜厚：均值及最小/最大范围", fontproperties=font_properties)
                axes.legend()
            else:
                durations = [float(row["duration_s"]) for row in duration_rows]
                if not all(math.isfinite(value) and value >= 0 for value in durations):
                    raise ValueError("层时长含无效值")
                axes.bar([row["layer"] for row in duration_rows], durations, color="#2788dd")
                axes.set_xlabel("Layer")
                axes.set_ylabel("Physical duration / s")
                axes.set_title("各层实际沉积时长", fontproperties=font_properties)
            axes.grid(True, alpha=.2)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(prefix=".plot-", suffix=".png", dir=directory, delete=False) as stream:
                    temporary = Path(stream.name)
                figure.savefig(temporary, format="png")
                replace_with_retry(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                figure.clear()
        record(name + ".png", target)
        if radial_rows:
            table("radial.csv", radial_rows)
        if duration_rows:
            table("layer_durations.csv", duration_rows)
    return {"ok": True, "run_id": run_id, "field_unit": unit, "coordinate_unit": "mm",
            "charts": selected, "files": files, "notes": notes,
            "radial_sampling": {"bins": radial_bins, "center_mm": list(center),
                                "weighting": "equal finite grid samples", "range": "min/max, not confidence interval"},
            "full_grid_used": True}
