"""Qt Quick controller for the existing thin-film spectral simulation service."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
import copy
import inspect
import json
import math
import os
import re
import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QObject, Property, QUrl, Signal, Slot
from PySide6.QtQml import QQmlApplicationEngine

from coat_sim.core.film_design import physical_thickness_nm, validate_design, validate_workflow_settings
from coat_sim.core.film_design_service import (
    FilmDesignService, estimate_film_simulation_resources, simulation_settings,
)
from coat_sim.core.optical_constants import prepare_optical_set
from coat_sim.core.film_optimization_preview import (
    PREVIEW_KIND,
    validate_optimization_preview_handoff,
)
from coat_sim.core.film_spectrum_analysis import calculate_spectrum_analysis
from coat_sim.ui.local_files import local_file_path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PARAMS = {
    "wavelength_min_nm": 400.0,
    "wavelength_max_nm": 700.0,
    "samples": 301,
    "step_nm": 1.0,
    "angle_deg": 0.0,
    "polarization": "unpolarized",
    "first_surface": "front",
    "light_source_id": "flat",
    "blackbody_temperature_k": 5778.0,
    "extrapolation_policy": "endpoint_hold",
}
_DEFAULT_PLOT_SETTINGS = {
    "curves": ["R"],
    "polarizations": [],
    "x": {"auto": True, "min": 400.0, "max": 700.0,
          "major_tick_auto": True, "major_tick": 50.0},
    "y": {"unit": "fraction", "auto": True, "min": 0.0, "max": 1.0,
          "major_tick_auto": True, "major_tick": 0.1},
    "grid": True,
}
_DEFAULT_PNG_EXPORT = {
    "width_in": 9.0,
    "height_in": 5.0,
    "dpi": 160,
    "transparent": False,
}
_CURVE_LABELS = {
    "R": "R（反射）", "T": "T（透射）", "A": "A（吸收）",
    "source_spectrum": "光源谱", "reflected_spectrum": "反射光谱",
    "transmitted_spectrum": "透射光谱", "absorbed_spectrum": "吸收光谱",
}
# Data-series colors are intentionally independent of the general UI theme.
_CURVE_COLORS = {
    "R": "#1769aa", "T": "#d97706", "A": "#16865b",
    "source_spectrum": "#7048a8", "reflected_spectrum": "#c04450",
    "transmitted_spectrum": "#168c9c", "absorbed_spectrum": "#9a6a20",
}
_MPL_FONT_PATHS_REGISTERED: set[str] = set()


def _normalize_plot_settings(options: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(options, dict):
        raise ValueError("绘图设置须为对象")
    merged = copy.deepcopy(_DEFAULT_PLOT_SETTINGS)
    allowed = set(_CURVE_LABELS)
    curves = options.get("curves", merged["curves"])
    if not isinstance(curves, (list, tuple)):
        raise ValueError("绘图曲线设置无效")
    merged["curves"] = [key for key in curves if key in allowed]
    polarizations = options.get("polarizations", merged["polarizations"])
    if (not isinstance(polarizations, (list, tuple))
            or any(item not in ("s", "p") for item in polarizations)):
        raise ValueError("Polarization curves must be s or p.")
    merged["polarizations"] = list(dict.fromkeys(polarizations))
    for name in ("x", "y"):
        current = options.get(name, {})
        if not isinstance(current, dict):
            raise ValueError(f"{name.upper()} 轴设置无效")
        axis = merged[name]
        axis["auto"] = bool(current.get("auto", axis["auto"]))
        axis["major_tick_auto"] = bool(current.get("major_tick_auto", axis["major_tick_auto"]))
        factor = 100.0 if name == "y" and current.get("unit") == "percent" else 1.0
        for key in ("min", "max", "major_tick"):
            if key in current:
                axis[key] = _finite(current[key], f"{name.upper()} 轴 {key}") / factor
        if not axis["auto"]:
            if axis["min"] >= axis["max"]:
                raise ValueError(f"{name.upper()} 轴最小值必须小于最大值。")
            if name == "x" and axis["min"] <= 0:
                raise ValueError("X 轴波长范围必须大于 0 nm。")
        if not axis["major_tick_auto"] and axis["major_tick"] <= 0:
            raise ValueError(f"{name.upper()} 轴主刻度间距必须大于 0。")
    merged["grid"] = bool(options.get("grid", True))
    return merged


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}须为有限数值")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}须为有限数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}须为有限数值")
    return number


def _count_for_step(span: float, step: float) -> int:
    if not math.isfinite(span) or span <= 0:
        raise ValueError("波长范围必须为有限正数。")
    if not math.isfinite(step) or step <= 0:
        raise ValueError("采样步长须为有限正数（nm）。")
    intervals = span / step
    if not math.isfinite(intervals):
        raise ValueError("采样点数超出当前运行时可寻址的数组长度")
    count = math.floor(intervals + 0.5) + 1
    if count < 2:
        raise ValueError("采样步长过大，结果少于 2 个波长点。")
    if count > sys.maxsize:
        raise ValueError("采样点数超出当前运行时可寻址的数组长度")
    return int(count)


def _display_indices(values: list[float], limit: int = 1800) -> list[int]:
    """Return extrema-preserving buckets for display only; never used by exports."""
    size = len(values)
    if size <= limit or limit < 4:
        return list(range(size))
    buckets = max(1, (limit - 2) // 2)
    result = {0, size - 1}
    for bucket in range(buckets):
        start = 1 + (size - 2) * bucket // buckets
        stop = 1 + (size - 2) * (bucket + 1) // buckets
        if stop <= start:
            continue
        section = values[start:stop]
        result.add(start + min(range(len(section)), key=section.__getitem__))
        result.add(start + max(range(len(section)), key=section.__getitem__))
    return sorted(result)


def _axis_ticks(minimum: float, maximum: float, option: dict[str, Any],
                *, target_count: int = 6, hard_limit: int = 80) -> list[float]:
    """Build a bounded tick list for the scenegraph preview."""
    span = maximum - minimum
    if not math.isfinite(span) or span <= 0:
        return []
    if not option.get("major_tick_auto", True):
        step = float(option.get("major_tick", 1.0))
    else:
        rough = span / max(1, target_count)
        power = 10.0 ** math.floor(math.log10(rough))
        normalized = rough / power
        nice = next((unit for unit in (1.0, 2.0, 2.5, 5.0, 10.0)
                     if normalized <= unit), 10.0)
        step = nice * power
    if not math.isfinite(step) or step <= 0:
        return []
    first_index = math.ceil(minimum / step - 1e-12)
    last_index = math.floor(maximum / step + 1e-12)
    count = max(0, last_index - first_index + 1)
    stride = max(1, math.ceil(count / hard_limit))
    return [float((first_index + offset * stride) * step)
            for offset in range(math.ceil(count / stride))]


def _calculate_band_statistics(result: dict[str, Any] | None,
                               minimum_nm: Any, maximum_nm: Any) -> dict[str, Any]:
    """Calculate band-limited R/T statistics without extrapolating the result."""
    if not result:
        return {"status": "no_result", "message": "尚无仿真结果。"}
    try:
        low = _finite(minimum_nm, "波段下限")
        high = _finite(maximum_nm, "波段上限")
    except ValueError as exc:
        return {"status": "invalid_range", "message": str(exc)}
    if low >= high:
        return {"status": "invalid_range", "message": "波段下限必须小于上限。"}

    try:
        waves = [_finite(value, "结果波长") for value in result.get("wavelength_nm", ())]
    except (TypeError, ValueError, OverflowError) as exc:
        return {"status": "invalid_result", "message": f"仿真波长数据无效：{exc}"}
    if len(waves) < 2 or any(right <= left for left, right in zip(waves, waves[1:])):
        return {"status": "invalid_result", "message": "仿真结果需要至少两个严格递增的波长点。"}
    if low < waves[0] or high > waves[-1]:
        return {
            "status": "out_of_range",
            "message": f"波段须落在已计算范围 {waves[0]:g}–{waves[-1]:g} nm 内；不会外推。",
        }

    def clipped_values(key: str) -> list[float]:
        raw_values = result.get(key)
        if not isinstance(raw_values, (list, tuple)) or len(raw_values) != len(waves):
            raise ValueError(f"仿真结果缺少匹配的 {key} 数据。")

        def interpolate(wavelength: float) -> float:
            index = bisect_left(waves, wavelength)
            if index < len(waves) and waves[index] == wavelength:
                return _finite(raw_values[index], f"{key} 数据")
            left, right = index - 1, index
            y0 = _finite(raw_values[left], f"{key} 数据")
            y1 = _finite(raw_values[right], f"{key} 数据")
            fraction = (wavelength - waves[left]) / (waves[right] - waves[left])
            return y0 + (y1 - y0) * fraction

        values = [interpolate(low)]
        first_inner = bisect_right(waves, low)
        after_inner = bisect_left(waves, high)
        values.extend(_finite(raw_values[index], f"{key} 数据")
                      for index in range(first_inner, after_inner))
        values.append(interpolate(high))
        return values

    try:
        clipped_waves = [low]
        clipped_waves.extend(wave for wave in waves if low < wave < high)
        clipped_waves.append(high)
        result_stats: dict[str, Any] = {}
        for key in ("R", "T"):
            values = clipped_values(key)
            area = sum((x1 - x0) * (y0 + y1) * 0.5
                       for x0, x1, y0, y1 in zip(
                           clipped_waves, clipped_waves[1:], values, values[1:]))
            result_stats.update({
                f"mean_{key}": area / (high - low),
                f"min_{key}": min(values),
                f"max_{key}": max(values),
            })
    except (TypeError, ValueError, OverflowError, IndexError) as exc:
        return {"status": "non_finite", "message": f"波段内 {exc}"}

    if any(not math.isfinite(value) for value in result_stats.values()):
        return {"status": "non_finite", "message": "波段统计结果包含非有限数值。"}
    return {"status": "ok", "message": "", "unit": "fraction", **result_stats}


def _simulate_with_polarizations(service: FilmDesignService, design: dict[str, Any],
                                 settings: dict[str, Any],
                                 requested: tuple[str, ...] | list[str],
                                 cancel_event=None) -> dict[str, Any]:
    """Keep the service result intact and attach only requested s/p display arrays."""
    def simulate(active_settings):
        try:
            parameters = inspect.signature(service.simulate).parameters.values()
            accepts_cancel = any(
                parameter.name == "cancel_event" or
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters)
        except (TypeError, ValueError):
            accepts_cancel = False
        if accepts_cancel:
            return service.simulate(design, active_settings, cancel_event=cancel_event)
        return service.simulate(design, active_settings)

    result = simulate(settings)
    current = result.get("settings", {}).get("polarization", settings.get("polarization"))
    polarization_curves: dict[str, dict[str, Any]] = {}
    for polarization in ("s", "p"):
        if polarization not in requested:
            continue
        if polarization == current:
            polarized = result
        else:
            polarized_settings = copy.deepcopy(settings)
            polarized_settings["polarization"] = polarization
            polarized = simulate(polarized_settings)
        polarization_curves[polarization] = {
            key: copy.deepcopy(polarized[key]) for key in ("R", "T", "A")
        }
    result["polarization_curves"] = polarization_curves
    return result


def _load_cie_light_sources(root: Path) -> list[dict[str, Any]]:
    """Load and validate the versioned, bundled CIE illuminant snapshot."""
    path = root / "data" / "film_design" / "cie-illuminants-20261006" / "data" / "cie_illuminants.json"
    if not path.is_file():
        return []
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
        if catalog.get("schemaVersion") != 1 or catalog.get("dataLicense") != "CC-BY-SA-4.0":
            return []
        entries = []
        for identifier in catalog.get("illuminantOrder", []):
            dataset = catalog.get("datasets", {}).get(identifier)
            if not isinstance(dataset, dict) or dataset.get("wavelengthBasis") != "standard_air":
                continue
            samples = dataset.get("samples")
            if not isinstance(samples, list) or len(samples) < 2 or len(samples) != dataset.get("sampleCount"):
                continue
            try:
                waves = [float(item[0]) for item in samples]
                levels = [float(item[1]) for item in samples]
            except (TypeError, ValueError, IndexError, OverflowError):
                continue
            if (any(not math.isfinite(item) for item in waves + levels)
                    or any(b <= a for a, b in zip(waves, waves[1:]))
                    or min(levels) < 0 or max(levels) <= 0):
                continue
            title = str(dataset.get("title", identifier))
            if dataset.get("kind") == "equal_energy_reference":
                title += " — E 等能参考（本包定义）"
            license_id = dataset.get("license", {}).get("spdx", catalog["dataLicense"])
            source = {
                "kind": "tabulated", "wavelength_nm": waves, "intensity": levels,
                "source": f"{title}; {dataset.get('citation', '')}; {dataset.get('doi', '')}; SPDX {license_id}",
                "wavelength_basis": "standard_air", "dataset_id": str(identifier),
                "snapshot_date": str(catalog.get("snapshotDate", "")),
                "data_license": str(license_id), "source_page": str(dataset.get("sourcePage", "")),
                "doi": str(dataset.get("doi", "")), "catalog_version": str(catalog.get("schemaVersion", "")),
                "source_warnings": [str(item) for item in dataset.get("warnings", []) if isinstance(item, str)],
            }
            entries.append({"token": "cie:" + str(identifier), "label": title, "source": source})
        return entries
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return []


def _channel_report(channel: str, data: dict[str, Any]) -> str:
    source = data.get("source_range_nm")
    if channel == "k" and (not data.get("enabled", True) or source is None):
        # Missing/disabled k remains a calculation policy, but is not a UI warning.
        return ""
    if source is None:
        return f"{channel}：来源范围未知"
    text = f"{channel}：来源范围 {source[0]:g}–{source[1]:g} nm"
    effective = data.get("effective_input_range_nm")
    if effective:
        text += f"；有效输入范围 {effective[0]:g}–{effective[1]:g} nm"
    method = data.get("interpolation_method") or data.get("method", "未知")
    method = {"linear": "分段线性插值", "piecewise_linear": "分段线性插值",
              "single_point": "单点数据", "cauchy": "Cauchy 公式",
              "sellmeier": "Sellmeier 公式"}.get(method, method)
    if str(method).startswith("RII formula "):
        method = "RII 公式 " + str(method).removeprefix("RII formula ")
    text += "；方法 " + str(method)
    segment = data.get("interpolated_segments_nm")
    if segment:
        text += f"；实际插值区间 {segment[0]:g}–{segment[1]:g} nm"
    # Do not expose k's zero-default intervals as warnings; the core result and
    # CSV provenance still retain them for auditing.
    outside = data.get("extrapolated_ranges_nm") or [] if channel != "k" else []
    if outside:
        text += "；外推范围 " + "、".join(f"{lo:g}–{hi:g} nm" for lo, hi in outside)
    elif channel == "k":
        text += "；不对 k 外推"
    return text


def _result_summary(result: dict[str, Any]) -> str:
    lines = ["数值诊断：n/k 来源区间、插值/外推范围与适用近似。"]
    handling = result.get("substrate_handling", {})
    if handling.get("average_fringes_approximation"):
        lines.append("厚基底采用平均条纹工程近似，保留体吸收与多次反射；不适用于激光或高分辨平行片测量。")
    seen = set()
    for item in result.get("provenance", []):
        evaluation = item.get("evaluation", {})
        for channel in ("n", "k"):
            data = evaluation.get(channel, {})
            if data:
                report = _channel_report(channel, data)
                if report and report not in seen:
                    seen.add(report)
                    lines.append(report)
    if result.get("warnings"):
        lines.append(f"计算提示共 {len(result['warnings'])} 项：")
        lines.extend("• " + str(item) for item in result["warnings"])
    return "\n".join(item for item in lines if item)


def _selected_plot_channels(result, settings):
    waves = result.get("wavelength_nm") or []
    base = result.get("settings", {}).get("polarization", "unpolarized")
    for key in settings.get("curves", []):
        if key not in _CURVE_LABELS:
            continue
        values = result.get(key)
        if isinstance(values, (list, tuple)) and len(values) == len(waves):
            yield key, base, values
        if key in ("R", "T", "A"):
            for polarization in settings.get("polarizations", []):
                if polarization == base:
                    continue
                values = result.get("polarization_curves", {}).get(polarization, {}).get(key)
                if isinstance(values, (list, tuple)) and len(values) == len(waves):
                    yield key, polarization, values


def _finite_curve_runs(waves, values):
    """Scan finite contiguous index runs without copying the full spectrum."""
    runs, start, count, minimum, maximum = [], None, 0, math.inf, -math.inf
    for index, (wave, value) in enumerate(zip(waves, values)):
        try:
            x, y = float(wave), float(value)
            valid = not isinstance(wave, bool) and not isinstance(value, bool) and math.isfinite(x) and math.isfinite(y) and x > 0
        except (ValueError, TypeError, OverflowError):
            valid = False
        if valid:
            if start is None:
                start = index
            count += 1
            minimum, maximum = min(minimum, x), max(maximum, x)
        elif start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(values)))
    return runs, count, minimum, maximum


def _display_curve_segments(waves, values, limit=1800):
    runs, count, minimum, maximum = _finite_curve_runs(waves, values)
    selected_runs = runs
    if len(runs) > limit // 2:
        capacity = limit // 2
        selected = {round(index * (len(runs) - 1) / (capacity - 1)) for index in range(capacity)}
        selected_runs = [runs[index] for index in sorted(selected)]
    total = sum(stop - start for start, stop in selected_runs)
    available = limit - sum(min(2, stop - start) for start, stop in selected_runs)
    segments = []
    for start, stop in selected_runs:
        size = stop - start
        budget = min(size, min(2, size) + math.floor(available * size / max(1, total)))
        if budget <= 2:
            indices = [start] if budget == 1 else [start, stop - 1]
        elif budget == 3:
            indices = [start, (start + stop) // 2, stop - 1]
        else:
            channel = values if start == 0 and stop == len(values) else values[start:stop]
            indices = [start + index for index in _display_indices(channel, budget)]
        segments.append([[float(waves[index]), float(values[index])] for index in indices])
    return segments, count


def _finite_plot_domain(result: dict[str, Any] | None,
                        settings: dict[str, Any]) -> dict[str, float]:
    """Union of selected, finite original samples, independent of display LOD."""
    x_min = y_min = math.inf
    x_max = y_max = -math.inf
    waves = (result or {}).get("wavelength_nm") or []
    for _key, _polarization, values in _selected_plot_channels(result or {}, settings):
        for wave, raw in zip(waves, values, strict=True):
            try:
                x, y = _finite(wave, "波长"), _finite(raw, "曲线值")
            except ValueError:
                continue
            if x <= 0:
                continue
            x_min, x_max = min(x_min, x), max(x_max, x)
            y_min, y_max = min(y_min, y), max(y_max, y)
    if not math.isfinite(x_min):
        return {}
    return {"x_min": x_min, "x_max": x_max, "y_min": y_min, "y_max": y_max}


def _bounded_pan_axis(low: float, high: float, offset: float,
                      domain_low: float, domain_high: float) -> tuple[float, float]:
    """Translate a zoomed view within data; wider/degenerate axes stay fixed."""
    span = high - low
    if span >= domain_high - domain_low:
        return low, high
    # Explicit empty user ranges are meaningful. Do not snap them to data on
    # a drag; applying settings and reset continue to own their exact bounds.
    if high < domain_low or low > domain_high:
        return low, high
    moved_low = min(domain_high - span, max(domain_low, low + offset))
    if moved_low == low:
        return low, high
    return moved_low, moved_low + span


def _automatic_y_limits(result: dict[str, Any] | None,
                        curves: list[str] | tuple[str, ...],
                        polarizations: list[str] | tuple[str, ...] = ()) -> tuple[float, float]:
    """Return padded fraction limits for every selected finite series."""
    low, high = math.inf, -math.inf
    payload = result or {}
    waves = payload.get("wavelength_nm") or []
    for _key, _polarization, channel in _selected_plot_channels(
            payload, {"curves": list(curves or ()), "polarizations": list(polarizations or ())}):
        for wave, raw in zip(waves, channel):
            try:
                x, value = _finite(wave, "波长"), _finite(raw, "曲线值")
            except ValueError:
                continue
            if x <= 0:
                continue
            low, high = min(low, value), max(high, value)

    if not math.isfinite(low) or not math.isfinite(high):
        return 0.0, 1.0

    span = high - low
    padding = span * 0.05 if span > 0 else max(abs(low) * 0.05, 0.01)
    lower, upper = low - padding, high + padding
    if 0.0 <= low and high <= 1.0:
        lower = max(0.0, lower)
        upper = min(1.0, upper)
    if upper <= lower:
        upper = lower + max(abs(lower) * 0.05, 0.01)
    return lower, upper


class FilmSpectrumPlotBridge(QObject):
    """Committed plot/export snapshot; PNG is rendered only when exported."""

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._result: dict[str, Any] | None = None
        self._settings = copy.deepcopy(_DEFAULT_PLOT_SETTINGS)
        self._theme_mode = "light"
        self._view_bounds: dict[str, float] | None = None

    def update(self, result: dict[str, Any] | None, settings: dict[str, Any],
               theme_mode: str = "light", view_bounds: dict[str, float] | None = None) -> None:
        self._result = result
        self._settings = copy.deepcopy(settings)
        self._theme_mode = theme_mode
        self._view_bounds = copy.deepcopy(view_bounds)

    @staticmethod
    def _render_png(result: dict[str, Any], settings: dict[str, Any], theme_mode: str,
                    *, display_limit: int | None, dpi: int, figure_size=(9.0, 5.0),
                    transparent=False, view_bounds: dict[str, float] | None = None) -> bytes:
        from io import BytesIO
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from matplotlib.ticker import MultipleLocator, PercentFormatter
        from matplotlib import font_manager
        from matplotlib.font_manager import FontProperties
        from coat_sim.ui.fonts import FONT_ROOT

        font_path = FONT_ROOT / "NotoSansCJKsc-Regular.otf"
        if font_path.is_file():
            font_key = str(font_path.resolve())
            if font_key not in _MPL_FONT_PATHS_REGISTERED:
                font_manager.fontManager.addfont(font_key)
                _MPL_FONT_PATHS_REGISTERED.add(font_key)
            font_properties = FontProperties(fname=font_key)
        else:
            installed = {entry.name for entry in font_manager.fontManager.ttflist}
            family = next((name for name in (
                "Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "Noto Sans CJK SC"
            ) if name in installed), "DejaVu Sans")
            font_properties = FontProperties(family=family)
        dark = theme_mode in ("dark", "high-contrast-dark")
        high_contrast = theme_mode.startswith("high-contrast")
        background = "#000000" if theme_mode == "high-contrast-dark" else "#14191e" if dark else "#ffffff"
        foreground = "#ffffff" if dark else "#000000" if high_contrast else "#25313b"
        grid_color = "#8a969f" if high_contrast else ("#58636d" if dark else "#aab4bd")

        figure = Figure(figsize=figure_size, dpi=dpi, facecolor=background)
        canvas = FigureCanvasAgg(figure)
        axes = figure.add_subplot(111)
        axes.set_facecolor(background)
        base_polarization = result.get("settings", {}).get("polarization", "unpolarized")
        selected_polarizations = [
            item for item in settings.get("polarizations", ()) if item in ("s", "p")
        ]
        polarization_curves = result.get("polarization_curves", {})
        for key in settings.get("curves", []):
            if key not in result or key not in _CURVE_LABELS:
                continue
            waves = result["wavelength_nm"]
            values = result[key]
            indices = _display_indices(values, display_limit) if display_limit else range(len(values))
            if key in ("R", "T", "A"):
                base_label = "非偏振" if base_polarization == "unpolarized" else base_polarization
                label = f"{_CURVE_LABELS[key]}（{base_label}）"
            else:
                label = _CURVE_LABELS[key]
            axes.plot([waves[i] for i in indices], [values[i] for i in indices],
                      label=label, color=_CURVE_COLORS[key], linewidth=1.55)
            if key not in ("R", "T", "A"):
                continue
            for polarization in selected_polarizations:
                if polarization == base_polarization:
                    continue
                channel = polarization_curves.get(polarization, {}).get(key)
                if not isinstance(channel, (list, tuple)) or len(channel) != len(waves):
                    continue
                overlay_indices = (_display_indices(channel, display_limit)
                                   if display_limit else range(len(channel)))
                axes.plot(
                    [waves[i] for i in overlay_indices],
                    [channel[i] for i in overlay_indices],
                    label=f"{_CURVE_LABELS[key]}（{polarization}）",
                    color=_CURVE_COLORS[key], linewidth=1.45,
                    linestyle="--" if polarization == "s" else ":",
                )
        axes.set_xlabel("波长（真空，nm）", fontproperties=font_properties, color=foreground)
        axes.set_ylabel("反射 / 透射 / 吸收（%）或相对光谱（%）",
                        fontproperties=font_properties, color=foreground)
        axes.tick_params(colors=foreground)
        for edge in axes.spines.values():
            edge.set_color(foreground)
        axes.yaxis.set_major_formatter(PercentFormatter(xmax=1.0))
        if settings.get("y", {}).get("auto", True):
            axes.set_ylim(*_automatic_y_limits(
                result, settings.get("curves", []), selected_polarizations))
        for axis, name in ((axes.xaxis, "x"), (axes.yaxis, "y")):
            option = settings.get(name, {})
            if not option.get("auto", True):
                limits = (option.get("min"), option.get("max"))
                (axes.set_xlim if name == "x" else axes.set_ylim)(*limits)
            if not option.get("major_tick_auto", True):
                tick = float(option.get("major_tick", 1))
                if tick > 0:
                    axis.set_major_locator(MultipleLocator(tick))
        if view_bounds is not None:
            axes.set_xlim(view_bounds["x_min"], view_bounds["x_max"])
            axes.set_ylim(view_bounds["y_min"], view_bounds["y_max"])
        elif not axes.lines and len(result.get("wavelength_nm", [])) >= 2:
            axes.set_xlim(result["wavelength_nm"][0], result["wavelength_nm"][-1])
        axes.grid(bool(settings.get("grid", True)), alpha=.28, color=grid_color)
        if axes.lines:
            legend = axes.legend(prop=font_properties)
            if legend:
                legend.get_frame().set_facecolor(background)
                legend.get_frame().set_edgecolor(grid_color)
                for label in legend.get_texts():
                    label.set_color(foreground)
        for label in axes.get_xticklabels() + axes.get_yticklabels():
            label.set_fontproperties(font_properties)
        axes.xaxis.get_offset_text().set_fontproperties(font_properties)
        axes.yaxis.get_offset_text().set_fontproperties(font_properties)
        # Fixed margins make the interactive QML cursor/pan viewport map to the
        # exact same plot rectangle on every render and export.
        figure.subplots_adjust(left=.16, right=.98, bottom=.16, top=.92)
        output = BytesIO()
        figure.savefig(output, format="png", dpi=dpi, transparent=transparent,
                       facecolor=background)
        return output.getvalue()

    def export_png(self, path: str, options: dict[str, Any] | None = None) -> str:
        if self._result is None:
            raise ValueError("没有可导出的光谱图")
        export = copy.deepcopy(options or _DEFAULT_PNG_EXPORT)
        target = Path(path).expanduser()
        if target.suffix.lower() != ".png":
            raise ValueError("光谱图导出须为 .png")
        target.parent.mkdir(parents=True, exist_ok=True)
        png = self._render_png(self._result, self._settings, self._theme_mode,
                               display_limit=None, dpi=export["dpi"],
                               figure_size=(export["width_in"], export["height_in"]),
                               transparent=export["transparent"], view_bounds=self._view_bounds)
        target.write_bytes(png)
        return str(target)


class _CalculationSignals(QObject):
    completed = Signal(object)


class _FrozenPreviewMaterials:
    """The independent preview reads only the optimization submission snapshot."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = {str(row["id"]): copy.deepcopy(row) for row in rows}

    def _records(self, material_ids=None) -> list[dict[str, Any]]:
        if material_ids is None:
            rows = self._rows.values()
        else:
            rows = (self._rows[item] for item in material_ids if item in self._rows)
        return [copy.deepcopy(row) for row in rows]

    def get(self, material_id: str) -> dict[str, Any]:
        if material_id not in self._rows:
            raise ValueError(f"frozen preview material missing: {material_id}")
        return copy.deepcopy(self._rows[material_id])


class FilmSimulationQuickController(QObject):
    """UI state and asynchronous adapter for :class:`FilmDesignService`."""

    stateChanged = Signal()
    plotDataChanged = Signal()
    plotViewChanged = Signal()
    cursorReadoutChanged = Signal()
    spectrumAnalysisChanged = Signal()
    calculationItemsChanged = Signal()
    confirmationRequested = Signal(str, str)
    closeConfirmed = Signal()

    def __init__(self, root=None, service=None, design=None, parent=None,
                 source_controller=None, *, project_context=None):
        super().__init__(parent)
        self.project_context = (project_context if project_context is not None
                                else getattr(source_controller, "project_context", None))
        self._project_compute = None
        self._project_operation = None
        self._project_frozen_snapshot = None
        self._pending_project_completion = None
        if self.project_context is not None:
            if service is not self.project_context.service:
                raise ValueError("Project spectrum requires its project optical service")
            if (source_controller is not None and
                    getattr(source_controller, "session", None) is not self.project_context.editor):
                raise ValueError("Project spectrum requires its project editor")
            from coat_sim.core.project_compute import ProjectComputeWorkspace
            self._project_compute = ProjectComputeWorkspace.for_project(
                self.project_context.host, self.project_context.binding)
            self._project_compute.register_thaw_callback(self._on_project_unfrozen)
        service_root = getattr(service, "root", None) if service is not None else None
        self.root = Path(root or service_root or PROJECT_ROOT).resolve()
        self.service = service or FilmDesignService(self.root, restrict_to_root=False)
        self._source_controller_ref = (weakref.ref(source_controller)
                                       if source_controller is not None else None)
        self._source_connections = []
        self._pending_window_open = None
        self._pending_save_path = None
        self._pending_close = False
        self._configuration_dirty = False
        self._last_source_configuration = None
        self._optimizer_owner_token = object()
        self._optimizer_snapshot_generation = -1
        self._optimizer_snapshot = None
        self._source_session = (getattr(source_controller, "session", None)
                                if source_controller is not None else
                                self.project_context.editor
                                if self.project_context is not None and design is None else None)
        self._source_alive = source_controller is not None
        self._source_design_revision = -1
        self._source_sync_error = ""
        if self._source_session is not None:
            source_snapshot = self._source_session.snapshot()
            design = source_snapshot.design
            self._source_design_revision = int(source_snapshot.design_revision)
        self._design = validate_design(design) if design is not None else self.service.new_design()
        self._design_path = str(source_snapshot.path or "") if self._source_session is not None else ""
        self._params = copy.deepcopy(_DEFAULT_PARAMS)
        self._plot_settings = copy.deepcopy(_DEFAULT_PLOT_SETTINGS)
        self._calculation_bands = [{
            "minimum_nm": self._params["wavelength_min_nm"],
            "maximum_nm": self._params["wavelength_max_nm"],
        }]
        self._calculation_items_cache: list[dict[str, Any]] | None = None
        self._analysis_channel = "T"
        self._analysis_polarization = "current"
        self._analysis_threshold_fraction = 0.5
        self._spectrum_analysis_cache: dict[str, Any] | None = None
        self._light_sources = [{"token": "flat", "label": "平坦光源", "source": {"kind": "flat"}},
                               {"token": "blackbody", "label": "黑体（相对归一化）",
                                "source": {"kind": "blackbody", "temperature_k": 5778.0}}]
        self._light_sources.extend(_load_cie_light_sources(self.root))
        self._source_payloads = {item["token"]: copy.deepcopy(item["source"])
                                 for item in self._light_sources}
        self._result: dict[str, Any] | None = None
        self._summary = "尚未计算。"
        self._status = "加载膜系后开始仿真。"
        self._error = ""
        self._busy = False
        self._stale = True
        self._generation = 0
        self._next_calculation_id = 0
        self._active_calculation_token = None
        self._pending_recompute = False
        self._closed = False
        self._theme_mode = "light"
        self._optimization_preview_notice = ""
        self._optimization_preview_targets_met: bool | None = None
        self._optimization_preview_handoff_id = ""
        self._optimization_preview_auto_started = False
        self._view_bounds: dict[str, float] | None = None
        self._cursor_readout = ""
        self._png_export_settings = copy.deepcopy(_DEFAULT_PNG_EXPORT)
        self._executor: ThreadPoolExecutor | None = None
        self._calculation_future = None
        self._calculation_cancel_event: threading.Event | None = None
        self._signals = _CalculationSignals(self)
        self._signals.completed.connect(self._finish_calculation)
        if source_controller is not None:
            changed = getattr(source_controller, "changed", None)
            if changed is not None and hasattr(changed, "connect"):
                changed.connect(self._on_source_changed)
                self._source_connections.append((changed, self._on_source_changed))
            applied = getattr(source_controller, "optimizationApplied", None)
            if applied is not None and hasattr(applied, "connect"):
                applied.connect(self._on_source_optimization_applied)
                self._source_connections.append(
                    (applied, self._on_source_optimization_applied))
            destroyed = getattr(source_controller, "destroyed", None)
            if destroyed is not None and hasattr(destroyed, "connect"):
                destroyed.connect(self._on_source_destroyed)
                self._source_connections.append((destroyed, self._on_source_destroyed))
        self.plotBridge = FilmSpectrumPlotBridge(self)
        self._plot_data_cache = self._build_plot_data()
        self.loadSimulationConfiguration(self._design.get("workflow_settings", {}).get("simulation", {}))
        self._last_source_configuration = copy.deepcopy(self._design.get("workflow_settings", {}).get("simulation", {}))
        if not self._last_source_configuration and self._design.get("first_surface") in ("front", "back"):
            self._params["first_surface"] = self._design["first_surface"]
        self._refresh_validation()

    @Property("QVariantMap", notify=stateChanged)
    def settings(self) -> dict[str, Any]:
        source = self._source_payload(self._params["light_source_id"])
        if source.get("kind") == "blackbody":
            source["temperature_k"] = self._params["blackbody_temperature_k"]
        span = self._params["wavelength_max_nm"] - self._params["wavelength_min_nm"]
        actual_step = span / (self._params["samples"] - 1) if self._params["samples"] > 1 else 0.0
        return {
            "wavelength_min_nm": self._params["wavelength_min_nm"],
            "wavelength_max_nm": self._params["wavelength_max_nm"],
            "samples": self._params["samples"], "step_nm": actual_step,
            "angle_deg": self._params["angle_deg"],
            "polarization": self._params["polarization"],
            "first_surface": self._params["first_surface"],
            "extrapolation_policy": self._params["extrapolation_policy"],
            "light_source": source,
        }

    @Property(str, notify=stateChanged)
    def sampleCountText(self) -> str:
        """Exact decimal form for QML, whose Number values lose large integers."""
        return str(self._params["samples"])

    @Property(str, notify=stateChanged)
    def resourceEstimate(self) -> str:
        try:
            estimate = estimate_film_simulation_resources(
                self._current_settings(), self._design,
                extra_polarizations=len(self._plot_settings.get("polarizations", ())))
            mib = estimate["estimated_bytes"] / (1024 * 1024)
            return (f"预计计算内存约 {mib:,.1f} MiB "
                    "（仅供参考；完整结果随点数增长）")
        except (ValueError, TypeError, OverflowError, KeyError):
            return "输入有效后可显示资源估计。"

    @Property("QVariantMap", notify=stateChanged)
    def plotSettings(self) -> dict[str, Any]:
        return copy.deepcopy(self._plot_settings)

    @Property("QVariantList", notify=stateChanged)
    def lightSources(self) -> list[dict[str, str]]:
        return [{"token": item["token"], "label": item["label"]} for item in self._light_sources]

    @Property(int, notify=stateChanged)
    def lightSourceIndex(self) -> int:
        token = self._params["light_source_id"]
        return next((i for i, item in enumerate(self._light_sources) if item["token"] == token), 0)

    @Slot(int, result="QVariantMap")
    def lightSourcePayload(self, index: int) -> dict[str, Any]:
        if not 0 <= index < len(self._light_sources):
            return {}
        source = copy.deepcopy(self._light_sources[index]["source"])
        if source.get("kind") == "blackbody":
            source["temperature_k"] = self._params["blackbody_temperature_k"]
        return source

    @Property(str, notify=stateChanged)
    def lightSourceNotes(self) -> str:
        source = self._source_payload(self._params["light_source_id"])
        kind = source.get("kind", "flat")
        if kind == "flat":
            return "平坦参考光源；未提供来源或标准说明。"
        if kind == "blackbody":
            return "黑体相对光谱模型；按峰值归一化，未提供外部来源或标准说明。"
        lines = ["来源：" + str(source.get("source", "未提供"))]
        warnings = source.get("source_warnings") or []
        lines.extend("来源提示：" + str(item) for item in warnings)
        return "\n".join(lines)

    @Property(str, notify=stateChanged)
    def designName(self) -> str:
        return self._design.get("name", "未命名膜系")

    @Property(str, notify=stateChanged)
    def designPath(self) -> str:
        return self._design_path

    @Property(bool, notify=stateChanged)
    def hasDesign(self) -> bool:
        substrate = self._design.get("substrate", {})
        return bool(substrate.get("material_id") and substrate.get("optical_set_id"))

    @Slot(str, result="QVariantList")
    def schematicLayers(self, side: str) -> list[dict[str, Any]]:
        if side not in ("front", "back"):
            return []
        rows = []
        reference_wavelength = float(self._design.get("reference_wavelength_nm", 550.0))
        for layer in self._design.get(side + "_layers", []):
            material = {}
            optical = {}
            physical_nm = None
            material_id = str(layer.get("material_id", ""))
            optical_set_id = str(layer.get("optical_set_id", ""))
            try:
                material = self.service.materials.get(material_id)
                optical = next(
                    item for item in material.get("optical_sets", [])
                    if item.get("id") == optical_set_id)
            except (ValueError, TypeError, KeyError, StopIteration):
                material, optical = {}, {}
            thickness = layer.get("thickness", {})
            try:
                if thickness.get("unit") in ("nm", "um"):
                    physical_nm = physical_thickness_nm(
                        layer, reference_wavelength, None)
                elif thickness.get("unit") == "qw" and optical:
                    prepared = prepare_optical_set(optical)
                    n_reference, _k_reference, _details = prepared(
                        reference_wavelength, "nm", include_k=False)
                    physical_nm = physical_thickness_nm(
                        layer, reference_wavelength, n_reference)
            except (ValueError, TypeError, KeyError, OverflowError):
                physical_nm = None
            rows.append({
                "id": str(layer.get("id", "")),
                "materialId": material_id,
                "opticalSetId": optical_set_id,
                "name": str(material.get("name") or layer.get("material_id") or "未选择材料"),
                "setName": str(optical.get("name") or layer.get("optical_set_id") or "未选择数据集"),
                "unit": str(layer.get("thickness", {}).get("unit", "nm")),
                "physicalNmValue": physical_nm,
            })
        return rows

    def _open_optimization(self, *, start: bool) -> str:
        if not self.hasDesign:
            return ""
        launcher = self.parent()
        source = (self._source_controller_ref()
                  if self._source_controller_ref is not None else None)
        paired_opener = getattr(launcher, "open_or_reuse_film_companion", None)
        if (source is not None and self._source_alive and callable(paired_opener)):
            self.refreshFromSource()
            return str(paired_opener("film_optimization", source, start=start) or "")
        detached_opener = getattr(launcher, "open_or_reuse_detached_film_companion", None)
        if callable(detached_opener):
            snapshot = self._optimization_snapshot()
            return str(detached_opener(
                "film_optimization", snapshot,
                owner_token=self._optimizer_owner_token, start=start,
            ) or "")
        opener = getattr(launcher, "open_window", None)
        if not callable(opener):
            return ""
        snapshot = self._optimization_snapshot()
        return str(opener("film_optimization", mode="new", payload=snapshot) or "")

    @Slot(result=str)
    def openOptimization(self) -> str:
        return self._open_optimization(start=False)

    @Slot(result=str)
    def openOptimizationAndStart(self) -> str:
        return self._open_optimization(start=True)

    @Property(bool, notify=stateChanged)
    def useK(self) -> bool:
        return bool(self._design.get("use_k", True))

    @Property(bool, notify=stateChanged)
    def busy(self) -> bool:
        return self._busy

    @Property(bool, notify=stateChanged)
    def stale(self) -> bool:
        return self._stale

    @Property(bool, notify=stateChanged)
    def hasResult(self) -> bool:
        return self._result is not None

    @Property(bool, notify=stateChanged)
    def canExport(self) -> bool:
        return self._result is not None and not self._stale and not self._busy

    @Property(str, notify=stateChanged)
    def resultSummary(self) -> str:
        return self._summary

    @Property("QVariantList", notify=calculationItemsChanged)
    def calculationItems(self) -> list[dict[str, Any]]:
        if self._calculation_items_cache is not None:
            return copy.deepcopy(self._calculation_items_cache)
        items = []
        for index, band in enumerate(self._calculation_bands):
            stats = _calculate_band_statistics(
                self._result, band.get("minimum_nm"), band.get("maximum_nm"))
            if self._result is not None and self._stale:
                stats = {**stats, "status": "stale",
                         "message": "结果对应旧参数；重新计算后更新此项。"}
            items.append({"index": index, **copy.deepcopy(band), **stats})
        self._calculation_items_cache = items
        return copy.deepcopy(items)

    def _invalidate_calculation_items(self) -> None:
        self._calculation_items_cache = None

    def _invalidate_spectrum_analysis(self) -> None:
        self._spectrum_analysis_cache = None

    def _analysis_polarizations(self) -> list[str]:
        result = self._result or {}
        base = str(result.get("settings", {}).get(
            "polarization", self._params["polarization"]))
        available = [base]
        curves = result.get("polarization_curves", {})
        if isinstance(curves, dict):
            available.extend(item for item in ("s", "p")
                             if item != base and item in curves)
        return list(dict.fromkeys(available))

    @Property("QVariantList", notify=stateChanged)
    def analysisPolarizations(self) -> list[str]:
        return self._analysis_polarizations()

    @Property(str, notify=stateChanged)
    def analysisPolarization(self) -> str:
        available = self._analysis_polarizations()
        if self._analysis_polarization in available:
            return self._analysis_polarization
        return available[0] if available else self._params["polarization"]

    @Property(str, notify=stateChanged)
    def analysisChannel(self) -> str:
        return self._analysis_channel

    @Property(float, notify=stateChanged)
    def spectrumThresholdPercent(self) -> float:
        return self._analysis_threshold_fraction * 100.0

    @Property("QVariantMap", notify=spectrumAnalysisChanged)
    def spectrumAnalysis(self) -> dict[str, Any]:
        if self._spectrum_analysis_cache is None:
            self._spectrum_analysis_cache = calculate_spectrum_analysis(
                self._result, self._analysis_channel, self.analysisPolarization,
                self._analysis_threshold_fraction)
        return copy.deepcopy(self._spectrum_analysis_cache)

    @Property(str, notify=stateChanged)
    def status(self) -> str:
        return self._error or self._status

    @Property(str, notify=stateChanged)
    def validationError(self) -> str:
        return self._error

    @Property(str, notify=stateChanged)
    def optimizationPreviewNotice(self) -> str:
        return self._optimization_preview_notice

    @Property(bool, notify=stateChanged)
    def hasOptimizationPreview(self) -> bool:
        return bool(self._optimization_preview_handoff_id)

    @Slot(result=bool)
    def startOptimizationPreviewCalculation(self) -> bool:
        if not self._optimization_preview_handoff_id or self._optimization_preview_auto_started:
            return False
        self._optimization_preview_auto_started = True
        self.startCalculation()
        return True

    @Property("QVariantMap", notify=plotViewChanged)
    def plotView(self) -> dict[str, Any]:
        bounds = self._plot_bounds()
        settings = self._plot_settings
        return {
            **bounds,
            "x_ticks": _axis_ticks(
                bounds["x_min"], bounds["x_max"], settings["x"]),
            "y_ticks": _axis_ticks(
                bounds["y_min"], bounds["y_max"], settings["y"]),
            "grid": bool(settings.get("grid", True)),
        }

    @Property("QVariantMap", notify=plotDataChanged)
    def plotData(self) -> dict[str, Any]:
        return self._plot_data_cache

    def _refresh_plot_data(self) -> None:
        self._plot_data_cache = self._build_plot_data()

    def _build_plot_data(self) -> dict[str, Any]:
        self._plot_data_result = self._result
        base_bounds = self._plot_base_bounds()
        result = self._result or {}
        waves = result.get("wavelength_nm") or []
        settings = self._plot_settings
        base_polarization = str(result.get("settings", {}).get(
            "polarization", self._params["polarization"]))
        polarization_curves = result.get("polarization_curves", {})
        series: list[dict[str, Any]] = []
        partial_data = self._result is not None and any(
            not isinstance(result.get(key), (list, tuple)) or len(result[key]) != len(waves)
            for key in settings.get("curves", []) if key in _CURVE_LABELS)
        for key, polarization, values in _selected_plot_channels(result, settings):
            display_segments, valid_count = _display_curve_segments(waves, values)
            partial_data = partial_data or valid_count < len(waves)
            if not valid_count:
                continue
            base_label = "非偏振" if polarization == "unpolarized" else polarization
            label = (f"{_CURVE_LABELS[key]}（{base_label}）"
                     if key in ("R", "T", "A") else _CURVE_LABELS[key])
            series.append({
                "label": label, "channel": key, "polarization": polarization,
                "color": _CURVE_COLORS[key],
                "dash": ("solid" if polarization == base_polarization else
                         "dash" if polarization == "s" else "dot"),
                "points": display_segments[0], "segments": display_segments,
                "validPointCount": valid_count, "fullPointCount": len(waves),
            })
        return {
            "data_x_min": base_bounds["x_min"],
            "data_x_max": base_bounds["x_max"],
            "data_y_min": base_bounds["y_min"],
            "data_y_max": base_bounds["y_max"],
            "series": series,
            "pan_bounds": _finite_plot_domain(self._result, settings),
            "dataStatus": ("empty" if not series else "partial" if partial_data else "complete"),
            "dataStatusText": ("所选曲线没有有效采样数据。" if not series and self._result is not None else
                               "部分采样无效，曲线已按有效区间分段；自动范围仅覆盖有效数据。"
                               if partial_data else ""),
        }

    @Property(str, notify=cursorReadoutChanged)
    def cursorReadout(self) -> str:
        return self._cursor_readout

    @Property("QVariantMap", notify=stateChanged)
    def pngExportSettings(self) -> dict[str, Any]:
        return copy.deepcopy(self._png_export_settings)

    @Property(str, notify=stateChanged)
    def designJson(self) -> str:
        return json.dumps(self._design, ensure_ascii=False, allow_nan=False)

    def _source_payload(self, token: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self._source_payloads[token])
        except KeyError as exc:
            raise ValueError("所选光源不可用") from exc

    def _raw_settings(self) -> dict[str, Any]:
        payload = self.settings
        # The controller stores an exact integer count. Its displayed step is
        # derived, so passing both would let float round-trip change that count.
        payload.pop("step_nm", None)
        return payload

    def _current_settings(self) -> dict[str, Any]:
        return simulation_settings(self._raw_settings())

    @Slot(result="QVariantMap")
    def exportSimulationConfiguration(self) -> dict[str, Any]:
        return {
            "schema_version": 1, "parameters": self._raw_settings(),
            "blackbody_temperature_k": self._params["blackbody_temperature_k"],
            "plot": copy.deepcopy(self._plot_settings),
            "calculation_bands": copy.deepcopy(self._calculation_bands),
            "analysis": {"channel": self._analysis_channel,
                         "polarization": self._analysis_polarization,
                         "threshold_fraction": self._analysis_threshold_fraction},
            "png_export": copy.deepcopy(self._png_export_settings),
        }

    def _prepare_configuration(self, configuration):
        raw = validate_workflow_settings({"simulation": configuration})["simulation"]
        if set(raw) - {"schema_version", "parameters", "blackbody_temperature_k", "plot",
                       "calculation_bands", "analysis", "png_export"}:
            raise ValueError("未知仿真配置字段")
        if type(raw.get("schema_version", 1)) is not int or raw.get("schema_version", 1) != 1:
            raise ValueError("不支持的仿真配置版本")
        settings = simulation_settings(raw.get("parameters", {}))
        params = copy.deepcopy(_DEFAULT_PARAMS)
        for key in ("wavelength_min_nm", "wavelength_max_nm", "samples", "step_nm", "angle_deg",
                    "polarization", "first_surface", "extrapolation_policy"):
            params[key] = settings[key]
        temperature = _finite(raw.get("blackbody_temperature_k", 5778.0), "黑体温度")
        if not 1 <= temperature <= 1e7:
            raise ValueError("黑体温度须为 1–1e7 K")
        source = settings["light_source"]
        params["blackbody_temperature_k"] = source.get("temperature_k", temperature)
        token = next((key for key, value in self._source_payloads.items() if value == source), "")
        if not token:
            token = source["kind"] if source["kind"] in ("flat", "blackbody") else "document:tabulated"
        params["light_source_id"] = token
        plot = _normalize_plot_settings(raw.get("plot", {}))
        bands = raw.get("calculation_bands", [{"minimum_nm": params["wavelength_min_nm"],
                                              "maximum_nm": params["wavelength_max_nm"]}])
        if not isinstance(bands, list) or len(bands) > 12:
            raise ValueError("计算波段须为最多12项的数组")
        for band in bands:
            if not isinstance(band, dict) or set(band) != {"minimum_nm", "maximum_nm"}:
                raise ValueError("计算波段字段无效")
            for value in band.values():
                if value is not None:
                    _finite(value, "计算波段")
        analysis = raw.get("analysis", {})
        if (not isinstance(analysis, dict) or set(analysis) - {"channel", "polarization", "threshold_fraction"}
                or analysis.get("channel", "T") not in ("R", "T")
                or analysis.get("polarization", "current") not in ("current", "s", "p", "unpolarized")):
            raise ValueError("光谱分析配置无效")
        threshold = _finite(analysis.get("threshold_fraction", 0.5), "通带阈值")
        if not 0 <= threshold <= 1:
            raise ValueError("通带阈值须为0–1")
        png = {**_DEFAULT_PNG_EXPORT, **raw.get("png_export", {})}
        if set(png) != set(_DEFAULT_PNG_EXPORT):
            raise ValueError("PNG配置字段无效")
        width, height = _finite(png["width_in"], "PNG width"), _finite(png["height_in"], "PNG height")
        if (not 4 <= width <= 18 or not 3 <= height <= 12 or type(png["dpi"]) is not int
                or not 72 <= png["dpi"] <= 300 or not isinstance(png["transparent"], bool)
                or width * height * png["dpi"] ** 2 > 40_000_000):
            raise ValueError("PNG导出尺寸/精度配置无效")
        return params, source, plot, bands, analysis, threshold, png

    def _apply_configuration(self, prepared) -> None:
        params, source, plot, bands, analysis, threshold, png = prepared
        token = params["light_source_id"]
        if token not in self._source_payloads or (source["kind"] == "tabulated" and self._source_payloads[token] != source):
            self._source_payloads[token] = copy.deepcopy(source)
            self._light_sources = [row for row in self._light_sources if row["token"] != token]
            self._light_sources.append({"token": token, "label": "文档表格光源", "source": copy.deepcopy(source)})
        physical_change = params != self._params
        self._params, self._plot_settings = params, plot
        self._calculation_bands = copy.deepcopy(bands)
        self._analysis_channel = analysis.get("channel", "T")
        self._analysis_polarization = analysis.get("polarization", "current")
        self._analysis_threshold_fraction = threshold
        self._png_export_settings = copy.deepcopy(png)
        self._invalidate_calculation_items()
        self._invalidate_spectrum_analysis()
        self._refresh_plot_data()
        self._render_plot()
        if physical_change:
            self._changed(message="已恢复仿真配置；可开始仿真。")
        self.plotDataChanged.emit()
        self.plotViewChanged.emit()
        self.calculationItemsChanged.emit()
        self.spectrumAnalysisChanged.emit()
        self.stateChanged.emit()

    @Slot("QVariantMap", result=bool)
    def loadSimulationConfiguration(self, configuration: dict) -> bool:
        try:
            prepared = self._prepare_configuration(configuration)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._error = f"无法载入仿真配置：{exc}"
            self.stateChanged.emit()
            return False
        self._apply_configuration(prepared)
        return True

    def _persist_configuration(self) -> None:
        configuration = self.exportSimulationConfiguration()
        self._last_source_configuration = copy.deepcopy(configuration)
        session = (self._source_session
                   if self._source_alive or self._project_draft_bound() else None)
        if session is not None:
            if not session.set_workflow_configuration("simulation", configuration):
                self._error = session.snapshot().error
                return
            self._design["workflow_settings"] = copy.deepcopy(
                session.snapshot().design.get("workflow_settings", {}))
        else:
            self._design.setdefault("workflow_settings", {})["simulation"] = configuration
            self._configuration_dirty = True

    @Property(bool, notify=stateChanged)
    def hasPendingOpen(self) -> bool:
        return self._pending_window_open is not None

    @Property(bool, notify=stateChanged)
    def documentDirty(self) -> bool:
        return self._configuration_dirty or bool(
            self._source_session is not None and self._source_session.snapshot().dirty)

    def reuseWindowOpen(self, document_path: str = "", payload: object = None) -> bool:
        if self._closed:
            return False
        if getattr(payload, "_source_closed", False):
            self._error = "来源设计窗口已关闭，未切换仿真文档。"
            self.stateChanged.emit()
            return False
        source_session = getattr(payload, "session", None)
        if self.project_context is not None and source_session is not None and (
                source_session is not self.project_context.editor
                or getattr(payload, "service", None) is not self.project_context.service):
            self._error = "不能把其他项目的来源绑定到此仿真窗口。"
            self.stateChanged.emit()
            return False
        if (document_path and self._design_path and
                Path(str(document_path)).resolve() == Path(self._design_path).resolve()):
            return True
        same_session = source_session is not None and source_session is self._source_session
        same_project_editor = (self.project_context is not None
                               and source_session is self.project_context.editor)
        if source_session is not None and (same_session or (
                same_project_editor and not self._configuration_dirty)):
            current_source = (self._source_controller_ref()
                              if self._source_controller_ref is not None else None)
            if current_source is not payload or not self._source_alive:
                self._rebind_source_observers(payload, source_session)
            self.refreshFromSource()
            return True
        if not document_path and payload is None:
            return True
        dirty = self._configuration_dirty or bool(
            self._source_session is not None and self._source_session.snapshot().dirty)
        if dirty:
            self._pending_window_open = (str(document_path), payload)
            self.confirmationRequested.emit("replace_document", "当前膜系工作流有未保存修改，仍要切换仿真文档吗？")
            self.stateChanged.emit()
            return True
        return self._apply_window_open_intent(str(document_path), payload)

    def _rebind_source_observers(self, source, session) -> None:
        """Reconnect presentation identity without replacing this same edit draft."""
        for signal, callback in self._source_connections:
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._source_connections.clear()
        self._source_controller_ref = weakref.ref(source)
        self._source_session = session
        self._source_alive = True
        for name, callback in (("changed", self._on_source_changed),
                               ("optimizationApplied", self._on_source_optimization_applied),
                               ("destroyed", self._on_source_destroyed)):
            signal = getattr(source, name, None)
            if signal is not None and hasattr(signal, "connect"):
                signal.connect(callback)
                self._source_connections.append((signal, callback))

    @Slot(bool)
    def resolvePendingAction(self, accepted: bool) -> None:
        if self._pending_close:
            self._pending_close = False
            if accepted:
                self._configuration_dirty = False
                self.closeConfirmed.emit()
            self.stateChanged.emit()
            return
        if self._pending_save_path is not None:
            path, self._pending_save_path = self._pending_save_path, None
            if accepted:
                self.saveFilePath(path, True)
            self.stateChanged.emit()
            return
        pending, self._pending_window_open = self._pending_window_open, None
        if accepted and pending is not None:
            document_path, payload = pending
            if self._apply_window_open_intent(document_path, payload) and getattr(payload, "session", None) is not None:
                self.requestCalculation()
        self.stateChanged.emit()

    @Slot(str, bool, result=bool)
    def saveFilePath(self, path: str, overwrite: bool = False) -> bool:
        if self._source_session is not None and self._source_alive:
            success = self._source_session.save_file(str(path), overwrite=overwrite)
            self._design_path = str(self._source_session.snapshot().path or "")
            self.stateChanged.emit()
            return bool(success)
        if not path:
            return False
        target = Path(path)
        if target.exists() and not overwrite:
            self._pending_save_path = str(target)
            self.confirmationRequested.emit("overwrite", "目标膜系文件已存在，是否覆盖？")
            return False
        try:
            normalized = self.service.save(target, self._design, overwrite=overwrite)
        except (ValueError, TypeError, OSError) as exc:
            self._error = str(exc)
            self.stateChanged.emit()
            return False
        self._design = normalized
        self._design_path = str(target)
        self._configuration_dirty = False
        self._error = ""
        self._status = "膜系及全部已提交工作流配置已保存。"
        self.stateChanged.emit()
        return True

    @Slot(result=bool)
    def saveCurrent(self) -> bool:
        if self.project_context is not None:
            return self.project_context.editor.save_current()
        return self.saveFilePath(self._design_path, True) if self._design_path else False

    @Slot(result=bool)
    def requestClose(self) -> bool:
        if self._closed or self._source_session is not None or not self._configuration_dirty:
            return True
        if not self._pending_close:
            self._pending_close = True
            self.confirmationRequested.emit("discard_changes", "关闭会放弃当前未保存的仿真配置，仍要关闭吗？")
        return False

    def _apply_window_open_intent(self, document_path: str, payload: object) -> bool:
        try:
            source_session = getattr(payload, "session", None)
            if self.project_context is not None and source_session is not None and (
                    source_session is not self.project_context.editor
                    or getattr(payload, "service", None) is not self.project_context.service):
                raise ValueError("不能把其他项目的来源绑定到此仿真窗口。")
            snapshot = source_session.snapshot() if source_session is not None else None
            candidate = (self.service.load(document_path) if document_path else
                         snapshot.design if snapshot is not None else payload)
            if isinstance(candidate, dict) and candidate.get("kind") == PREVIEW_KIND:
                return self.setOptimizationPreviewHandoff(candidate)
            service = getattr(payload, "service", self.service) if snapshot is not None else self.service
            normalized = service.validate(copy.deepcopy(dict(candidate)))
            configuration = normalized.get("workflow_settings", {}).get("simulation", {})
            prepared = self._prepare_configuration(configuration)
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            self._error = f"无法切换仿真膜系：{exc}"
            self.stateChanged.emit()
            return False
        self._detach_from_source()
        self.service = service
        self._design = normalized
        self._design_path = str(snapshot.path or "") if snapshot is not None else str(document_path)
        self._configuration_dirty = False
        self._last_source_configuration = copy.deepcopy(configuration)
        if snapshot is not None:
            self._source_controller_ref = weakref.ref(payload)
            self._source_session = source_session
            self._source_alive = True
            self._source_design_revision = int(snapshot.design_revision)
            for name, slot in (("changed", self._on_source_changed),
                               ("optimizationApplied", self._on_source_optimization_applied),
                               ("destroyed", self._on_source_destroyed)):
                signal = getattr(payload, name, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(slot)
                    self._source_connections.append((signal, slot))
        self._apply_configuration(prepared)
        if not configuration and normalized.get("first_surface") in ("front", "back"):
            self._params["first_surface"] = normalized["first_surface"]
        self._changed(clear_result=True, message="已载入膜系及工作流配置；可开始仿真。")
        return True

    @Slot()
    def requestCalculation(self) -> None:
        if self._closed:
            return
        if self._pending_window_open is not None:
            return
        self.refreshFromSource()
        if self._busy:
            # Same in-flight input already meets this intent. A changed input
            # gets one recompute after cancellation/old result completion.
            try:
                if (self._active_calculation_token[2] != self._signature()
                        or self._active_calculation_token[1] != self._generation
                        or self._active_calculation_token[3] != tuple(self._plot_settings.get("polarizations", ()))):
                    self._pending_recompute = True
            except (TypeError, ValueError):
                self._pending_recompute = True
            return
        self.startCalculation()

    def _signature(self, design=None, settings=None) -> str:
        physical_design = {key: value for key, value in (design or self._design).items()
                           if key != "workflow_settings"}
        return json.dumps([physical_design, settings or self._current_settings()],
                          sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _plot_bounds(self) -> dict[str, float]:
        if self._view_bounds is not None:
            return copy.deepcopy(self._view_bounds)
        return self._plot_base_bounds()

    def _plot_base_bounds(self) -> dict[str, float]:
        waves = (self._result or {}).get("wavelength_nm") or []
        x_axis = self._plot_settings["x"]
        if x_axis["auto"]:
            limits = [_finite_curve_runs(waves, values)[2:] for _key, _polarization, values in
                      _selected_plot_channels(self._result or {}, self._plot_settings)]
            limits = [(low, high) for low, high in limits if math.isfinite(low) and math.isfinite(high)]
            if limits:
                low, high = min(item[0] for item in limits), max(item[1] for item in limits)
            else:
                low = float(self._params["wavelength_min_nm"])
                high = float(self._params["wavelength_max_nm"])
            if low == high:
                padding = max(abs(low) * 1e-6, 1e-6)
                x_min, x_max = max(math.nextafter(0.0, math.inf), low - padding), high + padding
            else:
                x_min, x_max = low, high
        else:
            x_min, x_max = float(x_axis["min"]), float(x_axis["max"])
        y_axis = self._plot_settings["y"]
        if y_axis["auto"]:
            y_min, y_max = _automatic_y_limits(
                self._result, self._plot_settings.get("curves", []),
                self._plot_settings.get("polarizations", []))
        else:
            y_min, y_max = float(y_axis["min"]), float(y_axis["max"])
        return {"x_min": x_min, "x_max": x_max, "y_min": y_min, "y_max": y_max}

    @Slot(str)
    def setSpectrumAnalysisChannel(self, channel: str) -> None:
        if channel in ("R", "T") and channel != self._analysis_channel:
            self._analysis_channel = channel
            self._persist_configuration()
            self._invalidate_spectrum_analysis()
            self.spectrumAnalysisChanged.emit()
            self.stateChanged.emit()

    @Slot(str)
    def setSpectrumAnalysisPolarization(self, polarization: str) -> None:
        if polarization in self._analysis_polarizations():
            self._analysis_polarization = polarization
            self._persist_configuration()
            self._invalidate_spectrum_analysis()
            self.spectrumAnalysisChanged.emit()
            self.stateChanged.emit()

    @Slot(float)
    def setSpectrumAnalysisThresholdPercent(self, value: float) -> None:
        try:
            percent = _finite(value, "通带阈值")
            if not 0.0 <= percent <= 100.0:
                raise ValueError("通带阈值须在 0–100% 之间。")
        except ValueError as exc:
            self._error = str(exc)
            self.stateChanged.emit()
            return
        fraction = percent / 100.0
        if fraction != self._analysis_threshold_fraction:
            self._analysis_threshold_fraction = fraction
            self._persist_configuration()
            self._error = ""
            self._invalidate_spectrum_analysis()
            self.spectrumAnalysisChanged.emit()
            self.stateChanged.emit()

    def _render_plot(self) -> None:
        bounds = self._plot_bounds() if self._result is not None else None
        self.plotBridge.update(self._result, self._plot_settings, self._theme_mode, bounds)

    def _refresh_validation(self) -> None:
        if self._source_sync_error:
            self._error = self._source_sync_error
            return
        try:
            self._current_settings()
            if not self.hasDesign:
                self._error = "请加载包含基片材料与光学常数组的有效膜系。"
            else:
                self._error = ""
        except (ValueError, TypeError) as exc:
            self._error = str(exc)

    def _changed(self, *, clear_result=False, preserve_view=False,
                 message="参数已改变，请重新计算；已有光谱已过期") -> None:
        if self._calculation_cancel_event is not None:
            self._calculation_cancel_event.set()
        self._generation += 1
        self._stale = True
        self._invalidate_calculation_items()
        self.calculationItemsChanged.emit()
        if clear_result:
            self._result = None
            if not preserve_view:
                self._view_bounds = None
            self._cursor_readout = ""
            self._invalidate_spectrum_analysis()
            self._refresh_plot_data()
            self.plotDataChanged.emit()
            self.plotViewChanged.emit()
            self.cursorReadoutChanged.emit()
            self.spectrumAnalysisChanged.emit()
            self._render_plot()
        self._refresh_validation()
        self._status = message
        self.stateChanged.emit()

    @Slot()
    def _on_source_changed(self) -> None:
        if not self.refreshFromSource():
            # Saves and configuration-only changes can change path/dirty
            # without advancing the physical design revision.
            self.stateChanged.emit()

    @Slot()
    def _on_source_optimization_applied(self) -> None:
        self.refreshFromSource()
        if self._source_alive and not self._closed:
            self._pending_recompute = True
            self._start_pending_recompute_if_idle()

    def _detach_from_source(self) -> None:
        """Keep explicit simulation loads independent from the design session."""
        for signal, slot in self._source_connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._source_connections.clear()
        self._source_controller_ref = None
        self._source_session = None
        self._source_alive = False
        self._source_design_revision = -1
        self._source_sync_error = ""
        self._pending_recompute = False

    def _project_draft_bound(self) -> bool:
        """Use object ownership, never a matching document ID, for live drafts."""
        return (self.project_context is not None
                and self._source_session is self.project_context.editor)

    def _optimization_snapshot(self):
        if (self._optimizer_snapshot is not None
                and self._optimizer_snapshot_generation == self._generation):
            return self._optimizer_snapshot
        from coat_sim.ui.film_optimization_quick_controller import (
            FilmOptimizationSourceSnapshot,
        )
        self._optimizer_snapshot = FilmOptimizationSourceSnapshot(
            service=self.service,
            design=copy.deepcopy(self._design),
            design_revision=0,
            path=str(self._design_path or ""),
            layers_by_side={
                face: tuple(copy.deepcopy(self.schematicLayers(face)))
                for face in ("front", "back")
            },
            owner_token=self._optimizer_owner_token,
        )
        self._optimizer_snapshot_generation = self._generation
        return self._optimizer_snapshot

    @Slot(object)
    def _on_source_destroyed(self, *_args) -> None:
        self.sourceWindowClosed()

    @Slot()
    def sourceWindowClosed(self) -> None:
        """Invalidate a paired source so late operations cannot reuse it."""
        if not self._source_alive:
            return
        project_session = self._source_session if self._project_draft_bound() else None
        project_revision = self._source_design_revision
        self._detach_from_source()
        if project_session is not None:
            # Closing a presentation does not replace the project's working
            # owner. Manual calculation can read it without opening a window.
            self._source_session = project_session
            self._source_design_revision = project_revision
        self.stateChanged.emit()

    @Slot(result=bool)
    def refreshFromSource(self) -> bool:
        """Copy the latest unsaved draft from this simulation's exact source."""
        if (self._closed or self._source_session is None
                or not (self._source_alive or self._project_draft_bound())):
            return False
        try:
            snapshot = self._source_session.snapshot()
            source_error = str(getattr(snapshot, "error", "") or "").strip()
            if source_error:
                raise ValueError("当前膜系设计有错误，请先修正或清除：" + source_error)
            if bool(getattr(snapshot, "expression_busy", False)):
                raise ValueError("当前膜系表达式正在处理，请完成后重新计算")
            revision = int(snapshot.design_revision)
            normalized = self.service.validate(copy.deepcopy(dict(snapshot.design)))
            self._source_sync_error = ""
            configuration = copy.deepcopy(normalized.get("workflow_settings", {}).get("simulation", {}))
            if configuration != self._last_source_configuration:
                if not self.loadSimulationConfiguration(configuration):
                    raise ValueError(self._error or "来源仿真配置无效")
                self._last_source_configuration = configuration
            if "workflow_settings" in snapshot.design:
                self._design["workflow_settings"] = copy.deepcopy(snapshot.design["workflow_settings"])
            else:
                self._design.pop("workflow_settings", None)
            self._design_path = str(snapshot.path or "")
            if revision == self._source_design_revision:
                self._refresh_validation()
                return False
        except (ValueError, TypeError, RuntimeError) as exc:
            self._source_sync_error = f"无法同步来源膜系：{exc}"
            self._changed(clear_result=True, preserve_view=True,
                          message="来源膜系同步失败；请修正后重新计算。")
            return False
        self._source_design_revision = revision
        self._design = normalized
        if not normalized.get("workflow_settings", {}).get("simulation") and normalized.get("first_surface") in ("front", "back"):
            self._params["first_surface"] = normalized["first_surface"]
        self._design_path = str(snapshot.path or "")
        self._changed(clear_result=True, preserve_view=True,
                      message="来源膜系已更新；可开始仿真。")
        return True

    def _start_pending_recompute_if_idle(self) -> None:
        if not self._pending_recompute or self._busy or self._closed:
            return
        if self._source_controller_ref is not None and not self._source_alive:
            self._pending_recompute = False
            return
        self._pending_recompute = False
        self.startCalculation()

    @Slot(str, "QVariant")
    def setParameter(self, key: str, value: Any) -> None:
        """Update one validated input, keeping the legacy range/step priority."""
        try:
            candidate = copy.deepcopy(self._params)
            if key in ("wavelength_min_nm", "wavelength_max_nm"):
                candidate[key] = _finite(value, "波长")
                low, high = candidate["wavelength_min_nm"], candidate["wavelength_max_nm"]
                if low <= 0 or high <= low:
                    raise ValueError("波长范围须为正数，且最小值必须小于最大值。")
                count = _count_for_step(high - low, candidate["step_nm"])
                candidate["samples"] = count
                candidate["step_nm"] = (high - low) / (count - 1)
            elif key == "step_nm":
                step = _finite(value, "采样步长")
                low, high = candidate["wavelength_min_nm"], candidate["wavelength_max_nm"]
                count = _count_for_step(high - low, step)
                candidate["samples"] = count
                candidate["step_nm"] = (high - low) / (count - 1)
            elif key == "samples":
                if type(value) is int:
                    count = value
                elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
                    try:
                        count = int(value.strip(), 10)
                    except (ValueError, OverflowError) as exc:
                        raise ValueError("当前运行时无法表示此采样点数") from exc
                else:
                    if isinstance(value, bool):
                        raise ValueError("采样点必须为整数")
                    try:
                        numeric = float(value)
                    except (ValueError, TypeError, OverflowError) as exc:
                        raise ValueError("采样点必须为整数") from exc
                    if not math.isfinite(numeric) or not numeric.is_integer():
                        raise ValueError("采样点必须为整数")
                    count = int(numeric)
                if not 2 <= count <= sys.maxsize:
                    raise ValueError("采样点须至少为 2，且不超过当前运行时可寻址的数组长度")
                candidate["samples"] = count
                candidate["step_nm"] = ((candidate["wavelength_max_nm"] - candidate["wavelength_min_nm"])
                                         / (count - 1))
            elif key == "angle_deg":
                angle = _finite(value, "入射角")
                if not 0 <= angle < 90:
                    raise ValueError("入射角须处于 [0, 90)°。")
                candidate[key] = angle
            elif key == "polarization":
                if value not in ("s", "p", "unpolarized"):
                    raise ValueError("请选择 s、p 或非偏振。")
                candidate[key] = value
            elif key == "first_surface":
                if value not in ("front", "back"):
                    raise ValueError("请选择正面或背面作为首个入射面。")
                candidate[key] = value
            elif key == "extrapolation_policy":
                if value not in ("endpoint_hold", "error"):
                    raise ValueError("请选择 n 超范围策略。")
                candidate[key] = value
            elif key == "light_source_id":
                token = str(value)
                if token not in self._source_payloads:
                    raise ValueError("所选光源不可用。")
                candidate[key] = token
            elif key == "blackbody_temperature_k":
                temperature = _finite(value, "黑体温度")
                if not 1 <= temperature <= 1.0e7:
                    raise ValueError("黑体温度须介于 1 至 1e7 K。")
                candidate[key] = temperature
            else:
                raise ValueError(f"未知仿真参数：{key}")
            if candidate == self._params:
                return
            self._params = candidate
            self._persist_configuration()
            self._changed()
        except (ValueError, TypeError, OverflowError) as exc:
            self._error = str(exc)
            self.stateChanged.emit()

    @Slot(int)
    def setLightSourceIndex(self, index: int) -> None:
        if 0 <= index < len(self._light_sources):
            self.setParameter("light_source_id", self._light_sources[index]["token"])

    @Slot(result=bool)
    def addCalculationBand(self) -> bool:
        if len(self._calculation_bands) >= 12:
            self._error = "最多可添加 12 个计算波段。"
            self.stateChanged.emit()
            return False
        self._calculation_bands.append({
            "minimum_nm": self._params["wavelength_min_nm"],
            "maximum_nm": self._params["wavelength_max_nm"],
        })
        self._error = ""
        self._persist_configuration()
        self._invalidate_calculation_items()
        self.calculationItemsChanged.emit()
        self.stateChanged.emit()
        return True

    @Slot(int)
    def removeCalculationBand(self, index: int) -> None:
        if 0 <= index < len(self._calculation_bands):
            self._calculation_bands.pop(index)
            self._persist_configuration()
            self._error = ""
            self._invalidate_calculation_items()
            self.calculationItemsChanged.emit()
            self.stateChanged.emit()

    @Slot(int, "QVariant", "QVariant", result=bool)
    def setCalculationBand(self, index: int, minimum_nm: Any, maximum_nm: Any) -> bool:
        if not 0 <= index < len(self._calculation_bands):
            return False

        def bound(value: Any) -> float | None:
            try:
                return _finite(value, "波长")
            except ValueError:
                return None

        self._calculation_bands[index] = {
            "minimum_nm": bound(minimum_nm),
            "maximum_nm": bound(maximum_nm),
        }
        self._error = ""
        self._persist_configuration()
        self._invalidate_calculation_items()
        self.calculationItemsChanged.emit()
        self.stateChanged.emit()
        return True

    @Slot("QVariantMap", result=bool)
    def setSimulationSettings(self, raw: dict[str, Any]) -> bool:
        """Accept an existing film-service settings object, including tabulated sources."""
        try:
            params, source = self._prepare_simulation_parameters(raw)
            return self._commit_simulation_and_plot_settings(
                params, source, self._plot_settings,
                explicit_plot_application=False)
        except (ValueError, TypeError, OverflowError) as exc:
            self._error = str(exc)
            self.stateChanged.emit()
            return False

    def _prepare_simulation_parameters(self, raw: dict[str, Any]):
        """Normalize a settings draft without changing controller state."""
        if not isinstance(raw, dict):
            raise ValueError("仿真参数须为对象")
        payload = copy.deepcopy(raw)
        if isinstance(payload.get("samples"), str):
            raw_count = payload["samples"].strip()
            if re.fullmatch(r"[0-9]+", raw_count) is None:
                raise ValueError("采样点必须为整数")
            try:
                payload["samples"] = int(raw_count, 10)
            except (ValueError, OverflowError) as exc:
                raise ValueError("当前运行时无法表示此采样点数") from exc
        # Preserve an exact count when step is only its derived display value.
        # An independently edited step keeps service precedence.
        count = payload.get("samples")
        if type(count) is int and count >= 2 and "step_nm" in payload:
            low = _finite(payload.get("wavelength_min_nm", 400), "波长下限")
            high = _finite(payload.get("wavelength_max_nm", 700), "波长上限")
            derived_step = (high - low) / (count - 1)
            if payload["step_nm"] == derived_step:
                payload.pop("step_nm")
        normalized = simulation_settings(payload)
        source = normalized["light_source"]
        token = source["kind"] if source["kind"] in ("flat", "blackbody") else ""
        if source["kind"] == "tabulated":
            token = next((key for key, value in self._source_payloads.items()
                          if value == source), "")
            if not token:
                token = f"loaded:{len(self._source_payloads) + 1}"
        params = copy.deepcopy(self._params)
        params.update({
            "wavelength_min_nm": normalized["wavelength_min_nm"],
            "wavelength_max_nm": normalized["wavelength_max_nm"],
            "samples": normalized["samples"], "step_nm": normalized["step_nm"],
            "angle_deg": normalized["angle_deg"], "polarization": normalized["polarization"],
            "first_surface": normalized["first_surface"],
            "extrapolation_policy": normalized["extrapolation_policy"],
            "light_source_id": token,
        })
        if source["kind"] == "blackbody":
            params["blackbody_temperature_k"] = source["temperature_k"]
        return params, source

    def _commit_simulation_and_plot_settings(self, params, source, plot,
                                             *, explicit_plot_application=False):
        """Commit already-normalized drafts together; plot-only edits stay fresh."""
        simulation_changed = params != self._params
        plot_changed = plot != self._plot_settings
        if not simulation_changed and not plot_changed:
            previous_error = self._error
            self._error = ""
            if explicit_plot_application:
                self._view_bounds = None
                self._cursor_readout = ""
                self._render_plot()
                self.plotViewChanged.emit()
                self.cursorReadoutChanged.emit()
            if previous_error or explicit_plot_application:
                self.stateChanged.emit()
            return True

        token = params["light_source_id"]
        if (token not in self._source_payloads
                or (source["kind"] == "tabulated" and self._source_payloads[token] != source)):
            self._source_payloads[token] = copy.deepcopy(source)
            self._light_sources = [row for row in self._light_sources if row["token"] != token]
            label = "已加载表格光源" if token.startswith("loaded:") else "文档表格光源"
            self._light_sources.append({"token": token, "label": label,
                                        "source": copy.deepcopy(source)})

        self._params = copy.deepcopy(params)
        self._plot_settings = copy.deepcopy(plot)
        self._persist_configuration()
        self._error = ""

        if simulation_changed:
            self._changed()

        if plot_changed:
            # Applying plot options deliberately returns to the committed axis
            # configuration instead of preserving a temporary pan or zoom.
            self._view_bounds = None
            self._cursor_readout = ""
            self._refresh_plot_data()
            self.plotDataChanged.emit()
            self.plotViewChanged.emit()
            self.cursorReadoutChanged.emit()
            if self._result is not None:
                self._render_plot()

        elif explicit_plot_application:
            self._view_bounds = None
            self._cursor_readout = ""
            self._render_plot()
            self.plotViewChanged.emit()
            self.cursorReadoutChanged.emit()

        if plot_changed and not simulation_changed and self._result is not None and not self._stale:
            current_polarization = self._result.get("settings", {}).get(
                "polarization", self._params["polarization"])
            requested_channels = [
                item for item in plot["polarizations"]
                if item != current_polarization
                and item not in self._result.get("polarization_curves", {})
            ]
            if (requested_channels
                    and any(key in plot["curves"] for key in ("R", "T", "A"))):
                self._render_plot()
                self.startCalculation()
                return True

        if plot_changed and not simulation_changed:
            self._error = ""
            self._status = "绘图设置已应用。"
        self.stateChanged.emit()
        return True

    @Slot("QVariantMap", "QVariantMap", result=bool)
    def setSimulationAndPlotSettings(self, simulation: dict[str, Any],
                                     plot: dict[str, Any]) -> bool:
        """Validate both dialog pages before applying either one."""
        try:
            params, source = self._prepare_simulation_parameters(simulation)
            normalized_plot = _normalize_plot_settings(plot)
            return self._commit_simulation_and_plot_settings(
                params, source, normalized_plot,
                explicit_plot_application=True)
        except (ValueError, TypeError, OverflowError) as exc:
            self._error = str(exc)
            self.stateChanged.emit()
            return False

    @Slot("QVariantMap")
    def setDesign(self, design: dict[str, Any]) -> None:
        if isinstance(design, dict) and design.get("kind") == PREVIEW_KIND:
            self.setOptimizationPreviewHandoff(design)
            return
        self._apply_window_open_intent("", design)

    @Slot("QVariantMap", result=bool)
    def setOptimizationPreviewHandoff(self, payload: dict[str, Any]) -> bool:
        try:
            handoff = validate_optimization_preview_handoff(payload)
            self._detach_from_source()
            snapshot = copy.deepcopy(handoff["frozen_material_snapshot"])
            frozen_service = copy.copy(self.service)
            frozen_service.materials = _FrozenPreviewMaterials(snapshot["materials"])
            frozen_service.freeze_design = (
                lambda _design, _settings=None, value=snapshot: copy.deepcopy(value))
            self.service = frozen_service
            self._design = copy.deepcopy(handoff["design"])
            for key in ("wavelength_min_nm", "wavelength_max_nm", "samples",
                        "step_nm", "angle_deg", "polarization", "first_surface",
                        "extrapolation_policy"):
                if key in handoff["settings"]:
                    self._params[key] = copy.deepcopy(handoff["settings"][key])
            self._calculation_bands = [{
                "minimum_nm": self._params["wavelength_min_nm"],
                "maximum_nm": self._params["wavelength_max_nm"],
            }]
            plot_settings = handoff["plot_settings"]
            curves = plot_settings.get("curves", [])
            if not isinstance(curves, list) or not curves or any(
                    curve not in ("R", "T") for curve in curves):
                raise ValueError("optimization preview plot selection is invalid")
            self._plot_settings = copy.deepcopy(_DEFAULT_PLOT_SETTINGS)
            self._plot_settings["curves"] = list(curves)
            summary = handoff["optimization_summary"]
            self._optimization_preview_targets_met = summary["targets_met"]
            preview_label = (
                "Queue best candidate preview"
                if summary["state"] == "best_candidate"
                else "Optimization preview"
            )
            if summary["targets_met"]:
                self._optimization_preview_notice = (
                    f"{preview_label} · sampled targets met · "
                    f"{summary['objective_count']} objective(s)")
            else:
                self._optimization_preview_notice = (
                    f"{preview_label} · best candidate did not meet every sampled target · "
                    f"max normalized error {summary['max_normalized_error']:.4g}")
            self._optimization_preview_handoff_id = str(handoff["handoff_id"])
            self._optimization_preview_auto_started = False
            self._design_path = ""
            self._error = ""
            self._changed(clear_result=True,
                          message="Optimization result loaded in an independent preview.")
            return True
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._error = f"Optimization preview could not be opened: {exc}"
            self.stateChanged.emit()
            return False

    @Slot(str)
    def setDesignJson(self, design_json: str) -> None:
        try:
            self.setDesign(json.loads(design_json))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._error = f"膜系 JSON 无效：{exc}"
            self.stateChanged.emit()

    @Slot(str)
    def loadDesignFromPath(self, path: str) -> None:
        self._apply_window_open_intent(str(path), None)

    @Slot(QUrl, result=str)
    def localFilePath(self, url: QUrl) -> str:
        """Convert the QML url value in Python, where QUrl methods exist."""
        path = local_file_path(url)
        if not path:
            self._error = "请选择本地文件。"
            self.stateChanged.emit()
            return ""
        return path

    @Slot(bool)
    def setUseK(self, enabled: bool) -> None:
        if bool(enabled) == self.useK:
            return
        if self._source_session is not None and self._source_alive:
            self._source_session.set_design_value("use_k", bool(enabled))
            return
        design = copy.deepcopy(self._design)
        design["use_k"] = bool(enabled)
        try:
            self._design = self.service.validate(design)
            self._configuration_dirty = True
            self._changed(message="k 值使用策略已改变，请重新计算。")
        except (ValueError, TypeError) as exc:
            self._error = str(exc)
            self.stateChanged.emit()

    @Slot("QVariantMap", result=bool)
    def setPlotSettings(self, options: dict[str, Any]) -> bool:
        try:
            merged = _normalize_plot_settings(options)
            self._plot_settings = merged
            self._persist_configuration()
            self._refresh_plot_data()
            current_polarization = (self._result or {}).get("settings", {}).get(
                "polarization", self._params["polarization"])
            requested_channels = [
                item for item in merged["polarizations"]
                if item != current_polarization
                and item not in (self._result or {}).get("polarization_curves", {})
            ]
            if (self._result is not None and not self._stale and requested_channels
                    and any(key in merged["curves"] for key in ("R", "T", "A"))):
                self._view_bounds = None
                self._cursor_readout = ""
                self._render_plot()
                self.plotDataChanged.emit()
                self.plotViewChanged.emit()
                self.cursorReadoutChanged.emit()
                self.startCalculation()
                return True
            # Explicit settings application invalidates transient pan/zoom bounds.
            self._view_bounds = None
            self._cursor_readout = ""
            self.plotDataChanged.emit()
            self.plotViewChanged.emit()
            self.cursorReadoutChanged.emit()
            if self._result is not None:
                self._render_plot()
            self._error = ""
            self._status = "绘图设置已应用。"
            self.stateChanged.emit()
            return True
        except (ValueError, TypeError) as exc:
            self._error = str(exc)
            self.stateChanged.emit()
            return False

    @Slot(str)
    def setPlotTheme(self, mode: str) -> None:
        if mode not in ("light", "dark", "high-contrast-light", "high-contrast-dark"):
            return
        if mode != self._theme_mode:
            self._theme_mode = mode
            if self._result is not None:
                self._render_plot()

    def _set_view_bounds(self, bounds: dict[str, float]) -> None:
        if not all(math.isfinite(value) for value in bounds.values()):
            return
        if bounds["x_min"] <= 0 or bounds["x_max"] <= bounds["x_min"]:
            return
        if bounds["y_max"] <= bounds["y_min"]:
            return
        self._view_bounds = dict(bounds)
        self._render_plot()
        self.plotViewChanged.emit()

    @Slot(float, float, float)
    def zoomPlot(self, factor: float, x_fraction: float, y_fraction: float) -> None:
        if self._result is None or not math.isfinite(factor) or factor <= 0:
            return
        bounds = self._plot_bounds()
        x_fraction = min(1.0, max(0.0, x_fraction))
        y_fraction = min(1.0, max(0.0, y_fraction))
        x_anchor = bounds["x_min"] + x_fraction * (bounds["x_max"] - bounds["x_min"])
        y_anchor = bounds["y_min"] + y_fraction * (bounds["y_max"] - bounds["y_min"])
        factor = min(5.0, max(0.2, factor))
        self._set_view_bounds({
            "x_min": x_anchor + (bounds["x_min"] - x_anchor) * factor,
            "x_max": x_anchor + (bounds["x_max"] - x_anchor) * factor,
            "y_min": y_anchor + (bounds["y_min"] - y_anchor) * factor,
            "y_max": y_anchor + (bounds["y_max"] - y_anchor) * factor,
        })

    @Slot(float, float)
    def panPlot(self, x_fraction: float, y_fraction: float) -> None:
        if self._result is None or not math.isfinite(x_fraction) or not math.isfinite(y_fraction):
            return
        bounds = self._plot_bounds()
        if self._plot_data_result is not self._result:
            self._refresh_plot_data()
        domain = self._plot_data_cache.get("pan_bounds", {})
        if not domain:
            return
        x_span = bounds["x_max"] - bounds["x_min"]
        y_span = bounds["y_max"] - bounds["y_min"]
        x_min, x_max = _bounded_pan_axis(
            bounds["x_min"], bounds["x_max"], -x_fraction * x_span,
            domain["x_min"], domain["x_max"])
        y_min, y_max = _bounded_pan_axis(
            bounds["y_min"], bounds["y_max"], y_fraction * y_span,
            domain["y_min"], domain["y_max"])
        moved = {"x_min": x_min, "x_max": x_max, "y_min": y_min, "y_max": y_max}
        if moved != bounds:
            self._set_view_bounds(moved)

    @Slot()
    def resetPlotView(self) -> None:
        self._view_bounds = None
        self._cursor_readout = ""
        if self._result is not None:
            self._render_plot()
        self.plotViewChanged.emit()
        self.cursorReadoutChanged.emit()

    @Slot(float)
    def setPlotCursor(self, wavelength_nm: float) -> None:
        if self._result is None or not math.isfinite(wavelength_nm):
            return
        waves = self._result.get("wavelength_nm") or []
        if not waves:
            return
        index = min(len(waves) - 1, max(0, bisect_left(waves, wavelength_nm)))
        if index and abs(waves[index - 1] - wavelength_nm) <= abs(waves[index] - wavelength_nm):
            index -= 1
        parts = [f"λ = {waves[index]:g} nm"]
        base_polarization = (self._result.get("settings", {}).get(
            "polarization", "unpolarized"))
        polarization_curves = self._result.get("polarization_curves", {})
        for key in self._plot_settings.get("curves", []):
            values = self._result.get(key)
            if key in _CURVE_LABELS and values is not None and index < len(values):
                suffix = (f"（{base_polarization}）"
                          if key in ("R", "T", "A") else "")
                try:
                    value_text = f"{_finite(values[index], '曲线值') * 100:.3f}%"
                except ValueError:
                    value_text = "无有效采样"
                parts.append(f"{_CURVE_LABELS[key]}{suffix} {value_text}")
            if key not in ("R", "T", "A"):
                continue
            for polarization in self._plot_settings.get("polarizations", []):
                if polarization == base_polarization:
                    continue
                channel = polarization_curves.get(polarization, {}).get(key)
                if isinstance(channel, (list, tuple)) and index < len(channel):
                    try:
                        value_text = f"{_finite(channel[index], '曲线值') * 100:.3f}%"
                    except ValueError:
                        value_text = "无有效采样"
                    parts.append(
                        f"{_CURVE_LABELS[key]}（{polarization}） "
                        f"{value_text}")
        self._cursor_readout = "  ·  ".join(parts)
        self.cursorReadoutChanged.emit()

    @Slot()
    def clearPlotCursor(self) -> None:
        if self._cursor_readout:
            self._cursor_readout = ""
            self.cursorReadoutChanged.emit()

    @Slot("QVariantMap", result=bool)
    def setPngExportSettings(self, options: dict[str, Any]) -> bool:
        try:
            width = _finite(options.get("width_in"), "PNG width")
            height = _finite(options.get("height_in"), "PNG height")
            dpi_value = options.get("dpi")
            if isinstance(dpi_value, bool) or int(dpi_value) != float(dpi_value):
                raise ValueError("PNG DPI must be a whole number")
            dpi = int(dpi_value)
            transparent = options.get("transparent", False)
            if not isinstance(transparent, bool):
                raise ValueError("PNG transparency must be true or false")
            if not 4.0 <= width <= 18.0 or not 3.0 <= height <= 12.0:
                raise ValueError("PNG dimensions must be 4–18 by 3–12 inches")
            if not 72 <= dpi <= 300:
                raise ValueError("PNG DPI must be between 72 and 300")
            if width * height * dpi * dpi > 40_000_000:
                raise ValueError("PNG export exceeds the 40 megapixel limit")
            self._png_export_settings = {
                "width_in": width, "height_in": height, "dpi": dpi,
                "transparent": transparent,
            }
            self._error = ""
            self._persist_configuration()
            self._status = "PNG export settings applied."
            self.stateChanged.emit()
            return True
        except (ValueError, TypeError, OverflowError) as exc:
            self._error = str(exc)
            self.stateChanged.emit()
            return False

    def _begin_calculation(self):
        if self._busy:
            return None
        if self._closed:
            raise ValueError("仿真窗口已关闭")
        self._refresh_validation()
        if self._error:
            raise ValueError(self._error)
        settings = self._current_settings()
        design = copy.deepcopy(self._design)
        if self._project_compute is not None:
            from uuid import uuid4
            self._project_operation = self._project_compute.capture(uuid4().hex)
            self._project_frozen_snapshot = self.service.freeze_design(design, settings)
        self._next_calculation_id += 1
        token = (self._next_calculation_id, self._generation,
                 self._signature(design, settings),
                 tuple(self._plot_settings.get("polarizations", ())))
        self._active_calculation_token = token
        self._calculation_cancel_event = threading.Event()
        cancel_event = self._calculation_cancel_event
        self._busy = True
        self._stale = True
        self._invalidate_calculation_items()
        self.calculationItemsChanged.emit()
        # Keep the committed spectrum and user viewport until atomic result swap.
        self._cursor_readout = ""
        self._summary = "正在后台计算光谱…"
        self._status = "正在后台计算；可以继续修改参数。"
        self._render_plot()
        self.plotViewChanged.emit()
        self.cursorReadoutChanged.emit()
        self.stateChanged.emit()
        return token, design, settings, cancel_event

    @Property(bool, notify=stateChanged)
    def localWorkRunning(self) -> bool:
        future = self._calculation_future
        return future is not None and not future.done()

    @Slot()
    def cancelLocalWork(self) -> None:
        if self._calculation_cancel_event is not None:
            self._calculation_cancel_event.set()

    @Slot()
    def startCalculation(self) -> None:
        if self._busy or self._closed:
            return
        self.refreshFromSource()
        token = None
        try:
            started = self._begin_calculation()
            if started is None:
                return
            token, design, settings, cancel_event = started
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="film-spectrum-quick")
            requested = token[3]
            worker_service: Any = self.service
            if self._project_compute is not None:
                if self._project_frozen_snapshot is None or self._project_operation is None:
                    raise ValueError("Project spectrum input/operation snapshot is absent")
                worker_service = FilmDesignService.__new__(FilmDesignService)
                worker_service.materials = _FrozenPreviewMaterials(
                    self._project_frozen_snapshot["materials"])
                self._project_compute.register_job(
                    self._project_operation.operation_id, module_key="film_simulation",
                    service_owned=False, state="running",
                    metadata=self._project_compute.metadata(self._project_operation))
            future = self._executor.submit(
                _simulate_with_polarizations, worker_service, design, settings,
                requested, cancel_event)
            self._calculation_future = future
            signals_ref = weakref.ref(self._signals)
            project_compute, project_operation = self._project_compute, self._project_operation

            def completed(job):
                try:
                    payload = (token, job.result(), "")
                except Exception as exc:  # propagated through the queued signal
                    payload = (token, None, str(exc))
                if project_compute is not None and project_operation is not None:
                    project_compute.update_job(
                        project_operation.operation_id, "failed" if payload[2] else "succeeded")
                signals = signals_ref()
                if signals is not None:
                    try:
                        signals.completed.emit(payload)
                    except RuntimeError:
                        # Closing the window can destroy the QObject while the
                        # Python worker is still unwinding the core calculation.
                        pass

            future.add_done_callback(completed)
        except (ValueError, OSError, RuntimeError) as exc:
            if token is not None and token == self._active_calculation_token:
                self._active_calculation_token = None
                self._busy = False
                self._calculation_cancel_event = None
            self._error = str(exc)
            self._status = "仿真未启动。"
            self.stateChanged.emit()

    def _on_project_unfrozen(self) -> None:
        pending, self._pending_project_completion = self._pending_project_completion, None
        if pending is not None and not self._closed:
            self._finish_calculation(pending)

    def _persist_project_result(self, result, error) -> None:
        if self._project_compute is None or self._project_operation is None:
            return
        operation = self._project_operation
        folder = self._project_compute.owned_path(
            self.root / "data" / "results" / "film_simulation", allow_missing=True)
        folder.mkdir(parents=True, exist_ok=True)
        path = self._project_compute.destination(folder / (operation.operation_id + ".json"))
        public_result = ({key: value for key, value in result.items() if not key.startswith("_")}
                         if isinstance(result, dict) else None)
        payload = {"kind": "coat_sim.project_film_simulation_result", "version": 1,
                   "project_id": operation.session.project_id,
                   "project_generation": operation.session.generation,
                   "operation_id": operation.operation_id,
                   "input_snapshot": self._project_frozen_snapshot,
                   "result": public_result, "error": str(error or "")}
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(text + "\n", encoding="utf-8")
        temporary.replace(path)

    @Slot(object)
    def _finish_calculation(self, payload: object) -> None:
        token, result, error = cast(tuple[Any, dict[str, Any] | None, str], payload)
        if result is None and not error:
            error = "光谱任务没有返回有效结果。"
        if self._closed or token != self._active_calculation_token:
            return
        if self._project_compute is not None and self._project_operation is not None:
            self._project_compute.update_job(
                self._project_operation.operation_id, "failed" if error else "succeeded")
            if self._project_compute.editing_frozen:
                self._pending_project_completion = payload
                return
            if not self._project_compute.accepts(self._project_operation):
                self._active_calculation_token = None
                self._busy = False
                self._calculation_cancel_event = None
                return
            try:
                self._persist_project_result(result, error)
            except (ValueError, TypeError, OSError) as exc:
                error = f"项目光谱结果保存失败：{exc}"
        self._active_calculation_token = None
        self._busy = False
        self._calculation_cancel_event = None
        try:
            current_signature = self._signature()
        except (ValueError, TypeError):
            current_signature = ""
        current_polarizations = tuple(self._plot_settings.get("polarizations", ()))
        if (token[1] != self._generation or token[2] != current_signature
                or token[3] != current_polarizations):
            self._stale = True
            self._invalidate_calculation_items()
            self.calculationItemsChanged.emit()
            self._refresh_validation()
            self._status = "计算期间参数或膜系已改变，旧光谱已丢弃；请重新计算。"
            self.stateChanged.emit()
            self._start_pending_recompute_if_idle()
            return
        if error or result is None:
            self._stale = True
            self._invalidate_calculation_items()
            self.calculationItemsChanged.emit()
            self._error = str(error)
            self._summary = self._error
            self._status = "光谱计算失败。"
            self.stateChanged.emit()
            self._start_pending_recompute_if_idle()
            return
        self._result = result
        self._stale = False
        self._invalidate_spectrum_analysis()
        self._invalidate_calculation_items()
        self._cursor_readout = ""
        self._error = ""
        self._summary = _result_summary(result)
        self._refresh_plot_data()
        self._status = "计算完成。"
        self._render_plot()
        self.plotDataChanged.emit()
        self.plotViewChanged.emit()
        self.cursorReadoutChanged.emit()
        self.spectrumAnalysisChanged.emit()
        self.calculationItemsChanged.emit()
        self.stateChanged.emit()
        self._start_pending_recompute_if_idle()

    @Slot(str)
    def exportCsvToPath(self, path: str) -> None:
        if not self.canExport:
            self._error = "请先用当前参数完成计算后导出。"
            self.stateChanged.emit()
            return
        try:
            if self._project_compute is not None:
                target = self._project_compute.destination(path)
                export_service = FilmDesignService.__new__(FilmDesignService)
                export_service.paths = self._project_compute.service
                export_service.export_csv(target, self._result, overwrite=True)
            else:
                self.service.export_csv(path, self._result, overwrite=True)
            self._error = ""
            self._status = f"CSV 已导出：{path}"
        except (OSError, ValueError, TypeError) as exc:
            self._error = str(exc)
        self.stateChanged.emit()

    @Slot(str)
    def exportPngToPath(self, path: str) -> None:
        if not self.canExport:
            self._error = "请先用当前参数完成计算后导出。"
            self.stateChanged.emit()
            return
        try:
            if self._project_compute is not None:
                path = str(self._project_compute.destination(path))
            self.plotBridge.export_png(path, self._png_export_settings)
            self._error = ""
            self._status = f"PNG 已导出：{path}"
        except (OSError, ValueError, TypeError) as exc:
            self._error = str(exc)
        self.stateChanged.emit()

    @Slot()
    def detach_observers(self) -> None:
        """Close the per-window controller before launcher-owned destruction."""
        self.close()

    @Slot()
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending_project_completion = None
        if self._project_compute is not None:
            remove = getattr(self._project_compute, "unregister_thaw_callback", None)
            if callable(remove):
                remove(self._on_project_unfrozen)
        self._generation += 1
        if self._calculation_cancel_event is not None:
            self._calculation_cancel_event.set()
        self._active_calculation_token = None
        self._pending_recompute = False
        self._busy = False
        # Retire both the worker result and derived views as soon as the
        # window closes. A late completion is already token-gated below, and
        # must not leave a stale spectrum retained on a closed controller.
        self._result = None
        self._view_bounds = None
        self._cursor_readout = ""
        self._invalidate_spectrum_analysis()
        self._invalidate_calculation_items()
        self._refresh_plot_data()
        self._detach_from_source()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._busy = False
        self.stateChanged.emit()


def create_film_simulation_quick_window(root=None, service=None, design=None,
                                        geometry_settings=None):
    """Create the standalone Quick window without embedding a QWidget canvas.

    The caller owns the returned engine, window, and controller for the full
    window lifetime. Shared application launchers may instead attach their own
    engine and controller to the same QML file.
    """
    from coat_sim.ui.theme_service import ThemeService

    engine = QQmlApplicationEngine()
    controller = FilmSimulationQuickController(root=root, service=service, design=design)
    engine.rootContext().setContextProperty("FilmSimulation", controller)
    ThemeService.instance().attach_engine(engine, "Theme")
    qml_path = Path(__file__).resolve().parent / "qml" / "FilmSimulationQuick.qml"
    engine.load(QUrl.fromLocalFile(os.fspath(qml_path)))
    roots = engine.rootObjects()
    if not roots:
        raise RuntimeError(f"Could not load Qt Quick film simulation window: {qml_path}")
    from coat_sim.ui.window_geometry import prepare_window_geometry
    prepare_window_geometry(roots[0], "film_simulation", settings=geometry_settings)
    return engine, roots[0], controller
