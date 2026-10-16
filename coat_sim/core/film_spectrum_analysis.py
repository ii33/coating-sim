"""Display statistics for a committed thin-film spectrum result.

The analysis intentionally operates on the existing sampled result. It does
not request additional optical calculations or change exported data.
"""
from __future__ import annotations

import math
from typing import Any


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


def _group_equal_runs(waves: list[float], values: list[float]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    start = 0
    while start < len(values):
        stop = start + 1
        while stop < len(values) and values[stop] == values[start]:
            stop += 1
        runs.append({
            "first_index": start,
            "last_index": stop - 1,
            "wavelength_min_nm": waves[start],
            "wavelength_max_nm": waves[stop - 1],
            "wavelength_nm": (waves[start] + waves[stop - 1]) * 0.5,
            "value": values[start],
        })
        start = stop
    return runs


def _intervals_at_threshold(waves: list[float], values: list[float],
                             threshold: float) -> list[dict[str, Any]]:
    intervals: list[dict[str, Any]] = []
    active_start: float | None = waves[0] if values[0] >= threshold else None

    for index in range(len(waves) - 1):
        x0, x1 = waves[index], waves[index + 1]
        y0, y1 = values[index], values[index + 1]
        above0, above1 = y0 >= threshold, y1 >= threshold
        if above0 == above1:
            continue
        fraction = (threshold - y0) / (y1 - y0)
        crossing = x0 + fraction * (x1 - x0)
        if above0:
            if active_start is None:
                active_start = x0
            intervals.append({"start_nm": active_start, "end_nm": crossing})
            active_start = None
        else:
            active_start = crossing

    if active_start is not None:
        intervals.append({"start_nm": active_start, "end_nm": waves[-1]})

    for interval in intervals:
        interval["observed_width_nm"] = max(0.0, interval["end_nm"] - interval["start_nm"])
        interval["truncated_start"] = (
            interval["start_nm"] == waves[0] and values[0] >= threshold)
        interval["truncated_end"] = (
            interval["end_nm"] == waves[-1] and values[-1] >= threshold)
        interval["complete"] = not (
            interval["truncated_start"] or interval["truncated_end"])
        interval["width_nm"] = (
            interval["observed_width_nm"] if interval["complete"] else None)
    return intervals


def calculate_spectrum_analysis(
        result: dict[str, Any] | None,
        channel: str = "T",
        polarization: str | None = None,
        threshold_fraction: Any = 0.5) -> dict[str, Any]:
    """Return sampled extrema and threshold-pass intervals for R or T.

    Local extrema use exact-equality plateaus and require a sample on each
    side. Global extrema include the endpoints. Threshold crossings are
    linearly interpolated only between finite solved samples. These are UI
    conventions over the result grid, not additional physical predictions.
    """
    if not isinstance(result, dict) or not result:
        return {"status": "no_result", "message": "尚无仿真结果。"}
    if channel not in ("R", "T"):
        return {"status": "invalid_channel", "message": "请选择 R 或 T 曲线。"}
    try:
        threshold = _finite(threshold_fraction, "通带阈值")
    except ValueError as exc:
        return {"status": "invalid_threshold", "message": str(exc)}
    if not 0.0 <= threshold <= 1.0:
        return {"status": "invalid_threshold", "message": "通带阈值须在 0–100% 之间。"}

    settings = result.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    base_polarization = str(settings.get("polarization", "unpolarized"))
    chosen_polarization = polarization or base_polarization
    if chosen_polarization == base_polarization:
        raw_values = result.get(channel)
    else:
        polarization_curves = result.get("polarization_curves")
        if not isinstance(polarization_curves, dict):
            polarization_curves = {}
        selected = polarization_curves.get(chosen_polarization)
        raw_values = selected.get(channel) if isinstance(selected, dict) else None
        if raw_values is None:
            return {
                "status": "polarization_unavailable",
                "message": f"当前结果没有 {chosen_polarization} 偏振的 {channel} 曲线。",
            }

    try:
        waves = [_finite(value, "结果波长") for value in result.get("wavelength_nm", ())]
        values = [_finite(value, f"{channel} 数据") for value in (raw_values or ())]
    except (TypeError, ValueError, OverflowError) as exc:
        return {"status": "invalid_result", "message": f"光谱数据无效：{exc}"}
    if (len(waves) < 2 or len(values) != len(waves)
            or any(right <= left for left, right in zip(waves, waves[1:]))):
        return {
            "status": "invalid_result",
            "message": "光谱结果需要至少两个递增波长点，且 R/T 长度须与波长一致。",
        }

    minimum, maximum = min(values), max(values)
    minimum_runs = [run for run in _group_equal_runs(waves, values)
                    if run["value"] == minimum]
    maximum_runs = [run for run in _group_equal_runs(waves, values)
                    if run["value"] == maximum]
    local_peaks: list[dict[str, Any]] = []
    local_valleys: list[dict[str, Any]] = []
    runs = _group_equal_runs(waves, values)
    for run_index in range(1, len(runs) - 1):
        run = runs[run_index]
        previous = runs[run_index - 1]["value"]
        following = runs[run_index + 1]["value"]
        point = {key: run[key] for key in (
            "wavelength_min_nm", "wavelength_max_nm", "wavelength_nm", "value")}
        if run["value"] > previous and run["value"] > following:
            local_peaks.append({"kind": "peak", **point})
        elif run["value"] < previous and run["value"] < following:
            local_valleys.append({"kind": "valley", **point})

    intervals = _intervals_at_threshold(waves, values, threshold)
    try:
        angle_deg = _finite(settings.get("angle_deg", 0.0), "入射角")
    except ValueError:
        angle_deg = None
    return {
        "status": "ok",
        "message": "",
        "channel": channel,
        "polarization": chosen_polarization,
        "angle_deg": angle_deg,
        "wavelength_min_nm": waves[0],
        "wavelength_max_nm": waves[-1],
        "threshold_fraction": threshold,
        "global_min": minimum_runs,
        "global_max": maximum_runs,
        "local_peaks": local_peaks,
        "local_valleys": local_valleys,
        "long_wave_pass_intervals": intervals,
        "short_wave_pass_intervals": list(reversed(intervals)),
    }
