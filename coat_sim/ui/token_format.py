"""Compact, display-only token counts; persisted counts remain integer tokens."""

from __future__ import annotations

import math


def format_tokens(value: object, *, unavailable: str = "未提供") -> str:
    """Format a nonnegative count using decimal K/M without hiding unknown data."""
    if value is None or isinstance(value, bool):
        return unavailable
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return unavailable
    if not math.isfinite(number) or number < 0:
        return unavailable
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}".rstrip("0").rstrip(".") + " M"
    precision = 3 if number < 1_000 else 2
    return f"{number / 1_000:.{precision}f}".rstrip("0").rstrip(".") + " K"
