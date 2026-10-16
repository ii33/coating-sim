"""Analytic circle domain and explicit captured-node preview sampling (CPU only)."""

from __future__ import annotations

import math

import numpy as np

from .contract import AppearanceInputError


BOUNDARY_POLICY = "captured_node_mean_with_local_nearest_fallback"


def circle_node_mask(x_mm, y_mm, radius_mm: float) -> np.ndarray:
    """Match the authoritative process circle-domain tolerance exactly."""
    x, y = np.meshgrid(x_mm, y_mm)
    return x * x + y * y <= radius_mm * radius_mm * (1.0 + 1e-12)


def _signed_quadrant_area(x: float, y: float, radius: float) -> float:
    sign = math.copysign(1.0, x) * math.copysign(1.0, y)
    x, y = min(abs(x), radius), min(abs(y), radius)
    a = math.sqrt(max(0.0, radius * radius - y * y))
    if x <= a:
        return sign * x * y

    def integral(u):
        return 0.5 * (u * math.sqrt(max(0.0, radius * radius - u * u)) +
                      radius * radius * math.asin(min(1.0, u / radius)))

    return sign * (y * a + integral(x) - integral(a))


def circle_cell_area(xmin, xmax, ymin, ymax, radius_mm: float) -> float:
    """Exact disk/axis-aligned-rectangle intersection area in square mm."""
    q = _signed_quadrant_area
    area = (q(xmax, ymax, radius_mm) - q(xmin, ymax, radius_mm) -
            q(xmax, ymin, radius_mm) + q(xmin, ymin, radius_mm))
    return float(np.clip(area, 0.0, (xmax - xmin) * (ymax - ymin)))


def prepare_circle_cells(layers, radius_mm: float) -> dict:
    """Freeze actual per-layer cell means, local fallbacks and audit metadata.

    All input layers have already passed domain/grid validation. Exterior
    values are never accessed. The resulting cell arrays are immutable and
    only inside-circle cells are eligible for lookup.
    """
    if not layers:
        return {"metadata": {"policy": None, "boundary_cell_count": 0,
                             "boundary_area_mm2": 0.0,
                             "nearest_fallback_count": 0,
                             "nearest_fallback_area_mm2": 0.0,
                             "nearest_fallbacks": []}}
    x, y, valid = (layers[0][key] for key in ("x_mm", "y_mm", "valid_mask"))
    corners = (valid[:-1, :-1], valid[:-1, 1:], valid[1:, :-1], valid[1:, 1:])
    count = sum(mask.astype(np.int8) for mask in corners)
    closest_x = np.clip(0.0, x[:-1], x[1:])
    closest_y = np.clip(0.0, y[:-1], y[1:])
    active = closest_y[:, None] ** 2 + closest_x[None, :] ** 2 < radius_mm ** 2
    thickness = np.full((len(layers), y.size - 1, x.size - 1), np.nan, dtype=np.float64)
    for il, layer in enumerate(layers):
        # np.where prevents any invalid exterior value from entering the sum.
        d = np.where(valid, layer["thickness_nm"], 0.0)
        sums = d[:-1, :-1] + d[:-1, 1:] + d[1:, :-1] + d[1:, 1:]
        np.divide(sums, count, out=thickness[il], where=active & (count > 0))
    boundary = active & (count < 4)
    node_y, node_x = np.nonzero(valid)  # row-major, deterministic ties
    captured_x, captured_y = x[node_x], y[node_y]
    fallback_rows = []
    boundary_area = fallback_area = 0.0
    for iy, ix in zip(*np.nonzero(boundary)):
        area = circle_cell_area(x[ix], x[ix + 1], y[iy], y[iy + 1], radius_mm)
        boundary_area += area
        if count[iy, ix] > 0:
            continue
        anchor = np.array([closest_x[ix], closest_y[iy]], dtype=np.float64)
        diagonal = math.hypot(float(x[ix + 1] - x[ix]), float(y[iy + 1] - y[iy]))
        # The physical local cell diagonal is the limit, never a global grid
        # spacing. Restrict candidates before nearest-distance computation.
        local = ((np.abs(captured_x - anchor[0]) <= diagonal) &
                 (np.abs(captured_y - anchor[1]) <= diagonal))
        candidates = np.flatnonzero(local)
        if not candidates.size:
            raise AppearanceInputError("circle boundary cell has no captured node within its local grid diagonal")
        distances2 = ((captured_x[candidates] - anchor[0]) ** 2 +
                      (captured_y[candidates] - anchor[1]) ** 2)
        best = int(candidates[int(np.argmin(distances2))])
        distance = math.sqrt(float(np.min(distances2)))
        if distance > diagonal * (1.0 + 8.0 * np.finfo(np.float64).eps):
            raise AppearanceInputError("circle boundary nearest-node fallback exceeds its local grid diagonal")
        jy, jx = int(node_y[best]), int(node_x[best])
        for il, layer in enumerate(layers):
            thickness[il, iy, ix] = layer["thickness_nm"][jy, jx]
        fallback_area += area
        fallback_rows.append({"cell_yx": [int(iy), int(ix)],
                              "anchor_mm": anchor.tolist(),
                              "captured_node_yx": [jy, jx],
                              "captured_node_mm": [float(x[jx]), float(y[jy])],
                              "anchor_to_node_distance_mm": distance,
                              "local_cell_diagonal_mm": diagonal,
                              "area_mm2": area})
    if not np.isfinite(thickness[:, active]).all():
        raise AppearanceInputError("circle sampling left an unassigned interior surface cell")
    thickness.setflags(write=False)
    active.setflags(write=False)
    return {"x_mm": x, "y_mm": y, "active_cell_mask": active,
            "thickness_cell_nm": thickness,
            "metadata": {"policy": BOUNDARY_POLICY,
                         "interior_rule": "four_captured_node_arithmetic_mean",
                         "boundary_rule": "mean_of_valid_captured_corners; local_nearest_node_when_no_valid_corner",
                         "approximation": "project_preview_convention; boundary_thickness_is_not_measured",
                         "boundary_cell_count": int(np.count_nonzero(boundary)),
                         "boundary_area_mm2": float(boundary_area),
                         "boundary_area_fraction": float(boundary_area / (math.pi * radius_mm ** 2)),
                         "nearest_fallback_count": len(fallback_rows),
                         "nearest_fallback_area_mm2": float(fallback_area),
                         "nearest_fallbacks": fallback_rows}}


def circle_cell_index(x_mm, y_mm, hit_x_mm: float, hit_y_mm: float) -> tuple[int, int]:
    """Choose a frozen cell in world XY with safe float32 endpoint roundoff."""
    indices = []
    for axis, coordinate in ((x_mm, hit_x_mm), (y_mm, hit_y_mm)):
        tolerance = max(1.0, max(abs(float(axis[0])), abs(float(axis[-1])))) * 8.0 * np.finfo(np.float32).eps
        if (not math.isfinite(coordinate) or coordinate < axis[0] - tolerance or
                coordinate > axis[-1] + tolerance):
            raise AppearanceInputError("circle BSDF hit lies outside the captured XY grid")
        coordinate = min(float(axis[-1]), max(float(axis[0]), coordinate))
        index = int(np.searchsorted(axis, coordinate, side="right") - 1)
        indices.append(min(axis.size - 2, max(0, index)))
    return indices[1], indices[0]
