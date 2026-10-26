"""Film thickness presentation only; never use formatted text as solver input."""
from __future__ import annotations

import math
from PySide6.QtCore import QLocale


def format_film_thickness(value: float | None, decimals: int | None = None, *, trim: bool = True) -> str:
    if value is None:
        return "—"
    if decimals is None:
        from .theme_service import ThemeService
        decimals = ThemeService.instance().film_thickness_decimals
    if type(decimals) is not int or not 0 <= decimals <= 6:
        raise ValueError("film thickness display decimals must be from 0 to 6")
    value = float(value)
    if not math.isfinite(value):
        return str(value)
    locale = QLocale()
    text = locale.toString(value, "f", decimals)
    if value != 0 and locale.toDouble(text)[0] == 0:
        text = locale.toString(value, "e", decimals)
    mantissa, separator, exponent = text.lower().partition("e")
    point = locale.decimalPoint()
    if trim and point in mantissa:
        mantissa = mantissa.rstrip("0").rstrip(point)
    return mantissa + (separator + exponent if separator else "")
