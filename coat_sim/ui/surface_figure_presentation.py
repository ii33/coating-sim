"""Read-only presentation of the configured substrate's nominal geometry.

This is not a coating metrology, wavefront, or stress calculation.  It formats
only the substrate surface descriptor carried with a simulation result.
"""

from collections.abc import Mapping
import math
from typing import Any


_KIND_LABELS = {
    "flat": "平面(Flat)",
    "spherical": "球面(Spherical)",
    "asphere": "非球面(Aspheric)",
    "paraboloid": "抛物面(Paraboloid)",
}


def _settings_mapping(source: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(source, Mapping):
        return None
    for key in ("requested_settings", "effective_settings", "settings"):
        nested = source.get(key)
        if isinstance(nested, Mapping):
            return nested
    return source


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _format(value: float) -> str:
    return f"{value:.6g}"


def format_surface_figure_summary(
        settings: Mapping[str, Any] | None) -> list[str]:
    """Return bilingual, read-only rows for settings or a result settings map.

    The nominal height span reuses ``CoatService.surface_profile`` at its
    existing 181-point X-diameter default.  Its values are center-referenced
    geometric heights in mm, not measured post-coating surface errors.
    """
    source = _settings_mapping(settings)
    rows: list[str] = []
    surface: Mapping[str, Any] | None = None
    radius_mm: float | None = None

    if source is not None:
        profile = source.get("substrate_profile")
        if isinstance(profile, Mapping):
            candidate = profile.get("surface")
            if isinstance(candidate, Mapping):
                surface = candidate
            outline = profile.get("outline")
            if isinstance(outline, Mapping) and outline.get("kind") == "circle":
                radius_mm = _number(outline.get("radius_mm"))
        candidate = source.get("substrate_surface")
        if surface is None and isinstance(candidate, Mapping):
            surface = candidate
        if radius_mm is None:
            radius_mm = _number(source.get("substrate_radius_mm"))
        if surface is None and ("substrate_surface" in source or radius_mm is not None):
            # The simulation model's documented default is a flat surface.
            surface = {"kind": "flat"}

    kind = surface.get("kind") if surface is not None else None
    rows.append(
        "基片名义面型(Nominal substrate geometry)："
        + _KIND_LABELS.get(kind, "未提供")
    )

    if surface is not None:
        for key, label, unit in (
                ("radius_mm", "曲率半径(Radius of curvature)", "mm"),
                ("conic_k", "圆锥常数(Conic constant)", ""),
                ("a4", "四阶系数(A4 coefficient)", "mm⁻³"),
                ("a6", "六阶系数(A6 coefficient)", "mm⁻⁵"),
                ("a8", "八阶系数(A8 coefficient)", "mm⁻⁷"),
                ("sag_edge_mm", "边缘矢高(Edge sag)", "mm")):
            value = _number(surface.get(key))
            if value is not None:
                suffix = f" {unit}" if unit else ""
                rows.append(f"{label}：{_format(value)}{suffix}")

    if surface is not None and radius_mm is not None and radius_mm > 0:
        try:
            from coat_sim.core.api import CoatService

            profile_points = CoatService.surface_profile(surface, radius_mm, count=181)
            heights = [_number(point[1]) for point in profile_points]
            finite_heights = [value for value in heights if value is not None]
        except (ValueError, TypeError, OverflowError):
            finite_heights = []
        if finite_heights:
            rows.append(
                "名义高度采样范围(Sampled nominal height range)："
                f"{_format(min(finite_heights))}–{_format(max(finite_heights))} mm "
                "(X 直径 181 点，相对中心)"
            )
        else:
            rows.append(
                "名义高度采样范围(Sampled nominal height range)：不可用"
            )
    else:
        rows.append("名义高度采样范围(Sampled nominal height range)：未提供")

    rows.extend((
        "高度去趋势(Height detrending)：未执行（输入名义几何，不是测量面形）",
        "镀后实测面形(Coated surface metrology)：未提供",
        "ZYGO 波前(ZYGO wavefront)：未提供",
        "应力(Stress)：未提供",
    ))
    return rows
