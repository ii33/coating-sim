"""Pitch-circle geometry for an external sun/planet gear pair.

The center distance is half the sum of the two reference pitch diameters:
R = m * (Z_s + Z_p) / 2.  Zero is retained for legacy, stationary-center
configurations; it is not a physically realizable gear mesh.
"""

import math


def _teeth(value, label):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or int(value) != value or not 1 <= value <= 1000):
        raise ValueError(f"{label}必须是 1–1000 的整数")
    return int(value)


def _nonnegative(value, label):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or value < 0):
        raise ValueError(f"{label}必须是非负有限数值")
    return float(value)


def center_distance_from_module(module_mm, sun_teeth, planet_teeth):
    """Return orbit radius / pitch-circle center distance in millimetres."""
    module = _nonnegative(module_mm, "齿轮模数")
    sun = _teeth(sun_teeth, "太阳轮齿数")
    planet = _teeth(planet_teeth, "行星轮齿数")
    return module * (sun + planet) / 2.0


def module_from_center_distance(center_distance_mm, sun_teeth, planet_teeth):
    """Return gear module in millimetres from center distance and tooth counts."""
    distance = _nonnegative(center_distance_mm, "行星盘轨道半径")
    sun = _teeth(sun_teeth, "太阳轮齿数")
    planet = _teeth(planet_teeth, "行星轮齿数")
    return 2.0 * distance / (sun + planet)
