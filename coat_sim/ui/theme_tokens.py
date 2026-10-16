"""Semantic Qt Quick theme tokens and immutable snapshot construction.

This is the initial, deliberately small project contract. It keeps application
chrome and controls separate from scientific plots and material-data colors.
"""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Mapping


COLOR_TOKEN_KEYS = (
    "colorWindow",
    "colorSurface",
    "colorSurfaceRaised",
    "colorText",
    "colorTextMuted",
    "colorTextOnAccent",
    "colorBorder",
    "colorBorderStrong",
    "colorAccent",
    "colorAccentHover",
    "colorAccentPressed",
    "colorFocusRing",
    "colorSelection",
    "colorSuccess",
    "colorWarning",
    "colorError",
    "colorInfo",
    "colorDisabledText",
    "colorDisabledSurface",
    "colorScrim",
)

OPACITY_TOKEN_KEYS = (
    "opacityDisabled",
    "opacityHover",
    "opacityPressed",
    "opacityScrim",
)

THEME_TOKEN_KEYS = (
    *COLOR_TOKEN_KEYS,
    *OPACITY_TOKEN_KEYS,
    "fontFamily",
    "fontSizeSmall",
    "fontSizeBody",
    "fontSizeHeading",
    "fontSizeTitle",
    "fontWeightNormal",
    "fontWeightEmphasis",
    "lineHeightBody",
    "spaceXs",
    "spaceSm",
    "spaceMd",
    "spaceLg",
    "spaceXl",
    "controlHeightSm",
    "controlHeightMd",
    "controlHeightLg",
    "radiusSm",
    "radiusMd",
    "radiusLg",
    "borderWidth",
    "focusRingWidth",
    "durationFast",
    "durationNormal",
    "fontScale",
    "densityScale",
)

if len(THEME_TOKEN_KEYS) != 49 or len(set(THEME_TOKEN_KEYS)) != 49:
    raise RuntimeError("The theme contract must contain 49 unique tokens")

THEME_MODES = (
    "light",
    "dark",
    "high-contrast-light",
    "high-contrast-dark",
)

_PALETTES: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "light": MappingProxyType(
            {
                "colorWindow": "#F3F6FA",
                "colorSurface": "#FFFFFF",
                "colorSurfaceRaised": "#F8FAFC",
                "colorText": "#17212B",
                "colorTextMuted": "#526170",
                "colorTextOnAccent": "#FFFFFF",
                "colorBorder": "#D4DDE6",
                "colorBorderStrong": "#7C8B9A",
                "colorAccent": "#1769AA",
                "colorAccentHover": "#0E5A98",
                "colorAccentPressed": "#0B4A7C",
                "colorFocusRing": "#095DB2",
                "colorSelection": "#D8EAFE",
                "colorSuccess": "#176B3A",
                "colorWarning": "#805000",
                "colorError": "#B3261E",
                "colorInfo": "#155E8A",
                "colorDisabledText": "#687786",
                "colorDisabledSurface": "#E7ECF2",
                "colorScrim": "#000000",
            }
        ),
        "dark": MappingProxyType(
            {
                "colorWindow": "#14191E",
                "colorSurface": "#1D242B",
                "colorSurfaceRaised": "#27313A",
                "colorText": "#F2F5F7",
                "colorTextMuted": "#BAC3CB",
                "colorTextOnAccent": "#101820",
                "colorBorder": "#414D58",
                "colorBorderStrong": "#697784",
                "colorAccent": "#81C2F4",
                "colorAccentHover": "#A1D3FA",
                "colorAccentPressed": "#5CA6DF",
                "colorFocusRing": "#A8D7FF",
                "colorSelection": "#284B68",
                "colorSuccess": "#80D9A1",
                "colorWarning": "#FFD080",
                "colorError": "#FF938C",
                "colorInfo": "#92CDF2",
                "colorDisabledText": "#89939D",
                "colorDisabledSurface": "#2B333B",
                "colorScrim": "#000000",
            }
        ),
        "high-contrast-light": MappingProxyType(
            {
                "colorWindow": "#FFFFFF",
                "colorSurface": "#FFFFFF",
                "colorSurfaceRaised": "#FFFFFF",
                "colorText": "#000000",
                "colorTextMuted": "#202020",
                "colorTextOnAccent": "#FFFFFF",
                "colorBorder": "#000000",
                "colorBorderStrong": "#000000",
                "colorAccent": "#003F9E",
                "colorAccentHover": "#002F78",
                "colorAccentPressed": "#001F52",
                "colorFocusRing": "#000000",
                "colorSelection": "#FFFF00",
                "colorSuccess": "#005A20",
                "colorWarning": "#704000",
                "colorError": "#A00000",
                "colorInfo": "#004C70",
                "colorDisabledText": "#3C3C3C",
                "colorDisabledSurface": "#E6E6E6",
                "colorScrim": "#000000",
            }
        ),
        "high-contrast-dark": MappingProxyType(
            {
                "colorWindow": "#000000",
                "colorSurface": "#000000",
                "colorSurfaceRaised": "#101010",
                "colorText": "#FFFFFF",
                "colorTextMuted": "#E8E8E8",
                "colorTextOnAccent": "#000000",
                "colorBorder": "#FFFFFF",
                "colorBorderStrong": "#FFFFFF",
                "colorAccent": "#FFFF00",
                "colorAccentHover": "#FFF76A",
                "colorAccentPressed": "#E4DC00",
                "colorFocusRing": "#FFFF00",
                "colorSelection": "#FFFF00",
                "colorSuccess": "#9BFF9B",
                "colorWarning": "#FFE36B",
                "colorError": "#FF9B9B",
                "colorInfo": "#9CDEFF",
                "colorDisabledText": "#C8C8C8",
                "colorDisabledSurface": "#242424",
                "colorScrim": "#000000",
            }
        ),
    }
)

_BASE_METRICS: Mapping[str, float | int | str] = MappingProxyType(
    {
        "opacityDisabled": 0.48,
        "opacityHover": 0.08,
        "opacityPressed": 0.16,
        "opacityScrim": 0.42,
        # The launcher registers this bundled family before it constructs the
        # shared QML theme. Theme packages may still override this token.
        "fontFamily": "Noto Sans CJK SC",
        "fontSizeSmall": 10.0,
        "fontSizeBody": 12.0,
        "fontSizeHeading": 15.0,
        "fontSizeTitle": 18.0,
        "fontWeightNormal": 400,
        "fontWeightEmphasis": 600,
        "lineHeightBody": 1.4,
        "spaceXs": 4.0,
        "spaceSm": 8.0,
        "spaceMd": 12.0,
        "spaceLg": 16.0,
        "spaceXl": 24.0,
        "controlHeightSm": 28.0,
        "controlHeightMd": 36.0,
        "controlHeightLg": 44.0,
        "radiusSm": 3.0,
        "radiusMd": 6.0,
        "radiusLg": 10.0,
        "borderWidth": 1.0,
        "focusRingWidth": 2.0,
        "durationFast": 100,
        "durationNormal": 180,
    }
)

_FONT_SCALE_KEYS = (
    "fontSizeSmall",
    "fontSizeBody",
    "fontSizeHeading",
    "fontSizeTitle",
)
_DENSITY_SCALE_KEYS = (
    "spaceXs",
    "spaceSm",
    "spaceMd",
    "spaceLg",
    "spaceXl",
    "controlHeightSm",
    "controlHeightMd",
    "controlHeightLg",
    "radiusSm",
    "radiusMd",
    "radiusLg",
    "borderWidth",
    "focusRingWidth",
)


def build_theme_snapshot(
    mode: str = "light",
    *,
    font_scale: float = 1.0,
    density_scale: float = 1.0,
) -> dict[str, str | float | int]:
    """Build a fresh, flat map of the 49 semantic tokens.

    Font sizes are logical points. Layout metrics are Qt Quick logical pixels;
    Qt's high-DPI layer handles device-pixel scaling separately.
    """

    if mode not in THEME_MODES:
        raise ValueError(f"unsupported theme mode: {mode!r}")
    _validate_scale("font_scale", font_scale)
    _validate_scale("density_scale", density_scale)

    snapshot: dict[str, str | float | int] = dict(_PALETTES[mode])
    snapshot.update(_BASE_METRICS)
    for key in _FONT_SCALE_KEYS:
        snapshot[key] = float(snapshot[key]) * font_scale
    for key in _DENSITY_SCALE_KEYS:
        snapshot[key] = float(snapshot[key]) * density_scale
    snapshot["fontScale"] = float(font_scale)
    snapshot["densityScale"] = float(density_scale)

    if set(snapshot) != set(THEME_TOKEN_KEYS):
        raise RuntimeError("Theme snapshot does not match the 49-token contract")
    for key in COLOR_TOKEN_KEYS:
        value = snapshot[key]
        if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
            raise RuntimeError(f"Color token {key} must use #RRGGBB")
    return snapshot


def _validate_scale(name: str, value: float) -> None:
    minimum, maximum = ((2 / 3), (8 / 3)) if name == "font_scale" else (0.75, 2.0)
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number from {minimum:g} to {maximum:g}") from exc
    if not math.isfinite(numeric) or not minimum <= numeric <= maximum:
        raise ValueError(f"{name} must be a finite number from {minimum:g} to {maximum:g}")
