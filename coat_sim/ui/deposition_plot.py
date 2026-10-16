"""Pure coordinate mapping for clicks on the deposition thickness image."""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True)
class DepositionPlotSample:
    """Nearest raw grid value and its location in plot-item logical pixels."""

    x_mm: float
    y_mm: float
    thickness: float | None
    column: int
    row: int
    marker_x: float
    marker_y: float


def sample_nearest_grid(
    field: object,
    x_axis: object,
    y_axis: object,
    point_x: float,
    point_y: float,
    image_x: float,
    image_y: float,
    image_width: float,
    image_height: float,
) -> DepositionPlotSample | None:
    """Map a plot-local point to the nearest validly-addressed raw grid node.

    ``image_*`` describes the actual painted image rectangle in the chart
    item's logical coordinates. A point in the surrounding letterbox returns
    ``None``. Values are sampled directly from ``field``; no color texture,
    interpolation, or fallback search across invalid cells is used.
    """
    try:
        # Keep a view of large simulation arrays; one click should not copy the
        # full field just to read a single grid node.
        values = np.asarray(field)
        xs = np.asarray(x_axis, dtype=float)
        ys = np.asarray(y_axis, dtype=float)
        px, py, left, top, width, height = map(float, (
            point_x, point_y, image_x, image_y, image_width, image_height))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("deposition plot coordinates and axes must be numeric")

    if values.ndim != 2 or not values.size:
        raise ValueError("deposition field must be a non-empty 2-D array")
    if xs.ndim != 1 or ys.ndim != 1 or len(xs) != values.shape[1] or len(ys) != values.shape[0]:
        raise ValueError("deposition field shape must match its x/y axes")
    if not np.isfinite(xs).all() or not np.isfinite(ys).all():
        raise ValueError("deposition axes must contain finite coordinates")
    if not all(math.isfinite(value) for value in (px, py, left, top, width, height)):
        return None
    if width <= 0 or height <= 0:
        return None

    u = (px - left) / width
    v = (py - top) / height
    if u < 0.0 or u > 1.0 or v < 0.0 or v > 1.0:
        return None

    # QML axis labels span x[0]..x[-1] left-to-right and y[0]..y[-1]
    # bottom-to-top. Choose the nearest physical axis coordinate instead of
    # assuming equal grid spacing.
    x_at_pointer = float(xs[0] + u * (xs[-1] - xs[0]))
    y_at_pointer = float(ys[-1] - v * (ys[-1] - ys[0]))
    column = int(np.argmin(np.abs(xs - x_at_pointer)))
    row = int(np.argmin(np.abs(ys - y_at_pointer)))

    value = float(values[row, column])
    thickness = value if math.isfinite(value) else None
    marker_x = left + (column / max(1, values.shape[1] - 1)) * width
    marker_y = top + ((values.shape[0] - 1 - row) / max(1, values.shape[0] - 1)) * height
    return DepositionPlotSample(
        x_mm=float(xs[column]),
        y_mm=float(ys[row]),
        thickness=thickness,
        column=column,
        row=row,
        marker_x=marker_x,
        marker_y=marker_y,
    )
