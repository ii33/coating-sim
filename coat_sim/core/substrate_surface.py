"""Rotationally symmetric physical substrate surfaces in the local frame.

The substrate centre is at ``substrate_z_mm`` in the machine frame. Heights
returned here are offsets from that centre: h(0)=0. Positive spherical radius
or positive parabolic edge sag places the edge farther along +Z. The normal
points to the evaporation sources near Z=0, so a flat surface has n=(0,0,-1).
"""

from collections.abc import Mapping
import math

SURFACE_KINDS = ("flat", "spherical", "asphere", "paraboloid")


def _finite(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label}必须为有限数值")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须为有限数值") from exc
    if not math.isfinite(value):
        raise ValueError(f"{label}必须为有限数值")
    return value


def validate_substrate_surface(descriptor, substrate_radius_mm):
    """Return a canonical surface descriptor after checking its radial domain."""
    radius = _finite(substrate_radius_mm, "基片半径")
    if radius <= 0:
        raise ValueError("基片半径必须大于 0")
    if descriptor is None:
        return {"kind": "flat"}
    if not isinstance(descriptor, Mapping):
        raise ValueError("基片面型必须为配置对象")
    kind = descriptor.get("kind")
    if kind not in SURFACE_KINDS:
        raise ValueError("基片曲面须为 flat、spherical 或 asphere")
    required = {"flat": {"kind"},
                "spherical": {"kind", "radius_mm"},
                "asphere": {"kind", "radius_mm", "conic_k", "a4", "a6", "a8"},
                "paraboloid": {"kind", "sag_edge_mm"}}[kind]
    if set(descriptor) != required:
        raise ValueError(f"{kind} 面型字段应为：{', '.join(sorted(required))}")
    if kind == "flat":
        return {"kind": "flat"}
    if kind in ("spherical", "asphere"):
        roc = _finite(descriptor["radius_mm"], "球面曲率半径")
        if not 1e-6 <= abs(roc) <= 1e12:
            raise ValueError("曲率半径绝对值须在 1e-6–1e12 mm 范围内")
        if kind == "spherical":
            if not radius < abs(roc):
                raise ValueError("球面曲率半径的绝对值须大于基片半径")
            return {"kind": "spherical", "radius_mm": roc}
        conic_k = _finite(descriptor["conic_k"], "圆锥常数")
        a4 = _finite(descriptor["a4"], "A4")
        a6 = _finite(descriptor["a6"], "A6")
        a8 = _finite(descriptor["a8"], "A8")
        edge_radicand = 1.0 - (1.0 + conic_k) * (radius / roc) ** 2
        if not math.isfinite(edge_radicand) or edge_radicand <= 1e-12:
            raise ValueError("非球面圆锥项在全口径内须有实数且有限的法线")
        import numpy as np

        # Check the entire radial domain, not just the edge: the even-power
        # correction can make an interior ring the lowest point.
        rr = np.linspace(0.0, radius, 257)
        radicand = 1.0 - (1.0 + conic_k) * (rr / roc) ** 2
        hh = (rr * rr / roc / (1.0 + np.sqrt(radicand)) +
              a4 * rr ** 4 + a6 * rr ** 6 + a8 * rr ** 8)
        if not np.isfinite(hh).all() or np.max(np.abs(hh)) > 1e9:
            raise ValueError("非球面矢高超出 ±1e9 mm 范围")
        return {"kind": "asphere", "radius_mm": roc, "conic_k": conic_k,
                "a4": a4, "a6": a6, "a8": a8}
    sag = _finite(descriptor["sag_edge_mm"], "抛物面边缘矢高")
    if abs(sag) > 1e9:
        raise ValueError("抛物面边缘矢高绝对值不得超过 1e9 mm")
    return {"kind": "paraboloid", "sag_edge_mm": sag}


def surface_edge_sag_mm(substrate_radius_mm, descriptor):
    """Signed edge height offset from the substrate centre, in millimetres."""
    profile = validate_substrate_surface(descriptor, substrate_radius_mm)
    if profile["kind"] == "flat":
        return 0.0
    if profile["kind"] == "paraboloid":
        return profile["sag_edge_mm"]
    if profile["kind"] == "asphere":
        roc = profile["radius_mm"]
        r = float(substrate_radius_mm)
        root = math.sqrt(1.0 - (1.0 + profile["conic_k"]) * (r / roc) ** 2)
        return (r * r / roc / (1.0 + root) + profile["a4"] * r ** 4 +
                profile["a6"] * r ** 6 + profile["a8"] * r ** 8)
    roc = profile["radius_mm"]
    absolute = abs(roc)
    r = float(substrate_radius_mm)
    # Rationalized spherical sag avoids cancellation for a nearly flat sphere.
    return math.copysign(r * r / (absolute + math.sqrt(absolute * absolute - r * r)),
                         roc)


def surface_height_and_normal(local_x_mm, local_y_mm, substrate_radius_mm,
                              descriptor=None):
    """Return ``(h, nx, ny, nz)`` on the physical disk.

    Inputs may be scalars or broadcastable NumPy arrays. ``h`` is the centre
    referenced height in mm; ``n`` is a unit normal toward the sources.
    Points outside the disk return NaN in all four outputs.
    """
    import numpy as np

    profile = validate_substrate_surface(descriptor, substrate_radius_mm)
    radius = float(substrate_radius_mm)
    x, y = np.broadcast_arrays(np.asarray(local_x_mm, dtype=float),
                               np.asarray(local_y_mm, dtype=float))
    radial2 = x * x + y * y
    # Parametric ring points at exactly r=radius can land a few ULP outside
    # the disk after sin/cos roundoff. Keep those boundary samples valid.
    radius2 = radius * radius
    inside = (radial2 <= radius2 * (1.0 + 1e-12)) & np.isfinite(radial2)
    safe_radial2 = np.where(inside, np.minimum(radial2, radius2), 0.0)
    if profile["kind"] == "flat":
        height = np.zeros_like(x, dtype=float)
        gradient_x = np.zeros_like(x, dtype=float)
        gradient_y = np.zeros_like(x, dtype=float)
    elif profile["kind"] == "paraboloid":
        coefficient = profile["sag_edge_mm"] / (radius * radius)
        height = coefficient * safe_radial2
        gradient_x = 2.0 * coefficient * x
        gradient_y = 2.0 * coefficient * y
    elif profile["kind"] == "spherical":
        roc = profile["radius_mm"]
        absolute = abs(roc)
        root = np.sqrt(absolute * absolute - safe_radial2)
        sign = math.copysign(1.0, roc)
        height = sign * safe_radial2 / (absolute + root)
        gradient_x = sign * x / root
        gradient_y = sign * y / root
    else:
        roc = profile["radius_mm"]
        c = 1.0 / roc
        k = profile["conic_k"]
        root = np.sqrt(1.0 - (1.0 + k) * c * c * safe_radial2)
        a4, a6, a8 = profile["a4"], profile["a6"], profile["a8"]
        height = (c * safe_radial2 / (1.0 + root) + a4 * safe_radial2 ** 2 +
                  a6 * safe_radial2 ** 3 + a8 * safe_radial2 ** 4)
        radial_gradient = (c / root + 4.0 * a4 * safe_radial2 +
                           6.0 * a6 * safe_radial2 ** 2 +
                           8.0 * a8 * safe_radial2 ** 3)
        gradient_x = radial_gradient * x
        gradient_y = radial_gradient * y
    normal_length = np.sqrt(1.0 + gradient_x ** 2 + gradient_y ** 2)
    return (np.where(inside, height, np.nan),
            np.where(inside, gradient_x / normal_length, np.nan),
            np.where(inside, gradient_y / normal_length, np.nan),
            np.where(inside, -1.0 / normal_length, np.nan))
