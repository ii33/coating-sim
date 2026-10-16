"""Read-only presentation of a deposition result's uniformity statistics.

No field sampling or result mutation occurs here.  The absolute RMS deviation
is recovered from the existing, unrounded population CV and positive mean.
It is deviation about the mean, not the RMS of the thickness itself.
"""
from __future__ import annotations

import math
from numbers import Real


def _finite_number(value: object, *, nonnegative: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (nonnegative and number < 0):
        return None
    return number


def _display_value(value: float | None, unit: str) -> str:
    if value is None:
        return "未提供"
    if value == 0:
        value = 0.0  # Do not expose a floating-point negative-zero spelling.
    # Follow the shared display convention: three decimals, trimming zeros,
    # with scientific notation rather than rounding a nonzero value to zero.
    fixed = f"{value:.3f}"
    if value != 0 and float(fixed) == 0:
        mantissa, exponent = f"{value:.3e}".split("e")
        text = mantissa.rstrip("0").rstrip(".") + "e" + exponent
    else:
        text = fixed.rstrip("0").rstrip(".")
    return f"{text} {unit}"


def format_uniformity_rows(result: object) -> list[dict[str, str]]:
    """Return the fixed bilingual label/value grid for an existing result.

    ``process_simulator._field_metrics`` obtains mean and population standard
    deviation from the same finite ``field_nm`` samples, using ``np.std``'s
    default ddof=0. Its CV is std / mean * 100 for positive mean. Hence only
    positive finite mean and nonnegative finite CV support the inverse display:
    RMS deviation = mean_nm * cv_percent / 100. Missing or invalid inputs are
    not recomputed from the field or interpreted as zero.
    """
    if not isinstance(result, dict):
        return []
    mean = _finite_number(result.get("mean_nm"))
    cv = _finite_number(result.get("cv_percent"), nonnegative=True)
    rms = mean * (cv / 100.0) if mean is not None and mean > 0 and cv is not None else None
    if rms is not None and not math.isfinite(rms):
        rms = None
    relative_rms = cv if rms is not None else None
    values = (
        ("mean_nm", "平均膜厚 (Mean)：", mean, "nm"),
        ("min_nm", "最小膜厚 (Min)：", _finite_number(result.get("min_nm")), "nm"),
        ("max_nm", "最大膜厚 (Max)：", _finite_number(result.get("max_nm")), "nm"),
        ("pv_nm", "峰谷差 (PV)：", _finite_number(result.get("pv_nm"), nonnegative=True), "nm"),
        ("pv_percent", "相对峰谷差 (PV)：", _finite_number(result.get("pv_percent"), nonnegative=True), "%"),
        ("rms_nm", "均方根偏差 (RMS)：", rms, "nm"),
        ("rms_percent", "相对均方根偏差 (RMS)：", relative_rms, "%"),
    )
    return [{"key": key, "label": label, "value": _display_value(value, unit)}
            for key, label, value, unit in values]
