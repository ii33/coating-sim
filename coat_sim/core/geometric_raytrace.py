"""Versioned, power-only geometric ray tracing for a common XZ meridional plane.

Lengths are mm, wavelengths and coating thicknesses are nm, and refractive
indices use the convention n + i*k. Rays travel forward through ordered plane
or axis-centered spherical interfaces. Every coating is evaluated as a local
coherent planar stack with :func:`film_optics.coherent_rta`; phase between
separate surfaces, reflected branches, diffraction, and Jones fields are not
modeled.

The only supported polarization frames share one incidence plane (XZ): s is
fixed along +Y, and p lies in XZ. The s and p powers of an unpolarized ray are
carried independently across all surfaces. This preserves the polarization
state's power split instead of resetting it at each interface. This intensity
model is exact only for such a common meridional plane with isotropic media.
"""
from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

import numpy as np

from coat_sim.core.film_optics import coherent_rta


SCHEMA = "coat_sim.geometric_raytrace"
SCHEMA_VERSION = 1
RESULT_SCHEMA = "coat_sim.geometric_raytrace_result"
_TOL = 1e-10
_SURFACE_SPACING_TOL_MM = 1e-10

_DEFAULT_LIMITS = {
    "max_samples": 20000,
    "max_wavelengths": 512,
    "max_surfaces": 32,
    "max_layers": 128,
    "max_paths": 100000,
    "max_interactions": 500000,
    "max_detector_cells": 1000000,
    "max_result_bytes": 128 * 1024 * 1024,
    "max_asset_bytes": 512 * 1024 * 1024,
    "batch_paths": 128,
}
_HARD_LIMITS = {
    "max_samples": 250000,
    "max_wavelengths": 10000,
    "max_surfaces": 128,
    "max_layers": 512,
    "max_paths": 250000,
    "max_interactions": 1000000,
    "max_detector_cells": 4000000,
    "max_result_bytes": 512 * 1024 * 1024,
    "max_asset_bytes": 512 * 1024 * 1024,
    "batch_paths": 4096,
}


def _object(value: Any, name: str, required: set[str], optional: set[str] = frozenset()):
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing:
        raise ValueError(f"{name} is missing required field(s): {', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"{name} has unsupported field(s): {', '.join(sorted(extra))}")
    return value


def _number(value: Any, name: str, *, minimum: float | None = None,
            maximum: float | None = None) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be >= {minimum:g}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be <= {maximum:g}")
    return result


def _pair(value: Any, name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} must contain exactly two numbers")
    return (_number(value[0], f"{name}[0]"), _number(value[1], f"{name}[1]"))


def _index(value: Any, name: str, *, transparent: bool = False) -> complex:
    item = _object(value, name, {"n", "k"})
    n = _number(item["n"], f"{name}.n", minimum=np.nextafter(0.0, 1.0))
    k = _number(item["k"], f"{name}.k", minimum=0)
    if transparent and k != 0:
        raise ValueError(f"{name}.k must be zero for geometrical bulk propagation")
    return complex(n, k)


def _index_rows(value: Any, count: int, name: str, *, transparent: bool = False) -> list[complex]:
    if not isinstance(value, list) or len(value) != count:
        raise ValueError(f"{name} must contain one index for every spectrum entry")
    return [_index(item, f"{name}[{i}]", transparent=transparent)
            for i, item in enumerate(value)]


def _unique_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > 128:
        raise ValueError(f"{name} must be at most 128 characters")
    return value


def _request_limits(raw: Any) -> dict[str, int]:
    if raw is None:
        return dict(_DEFAULT_LIMITS)
    item = _object(raw, "limits", set(), set(_DEFAULT_LIMITS))
    limits = dict(_DEFAULT_LIMITS)
    for name, value in item.items():
        hard = _HARD_LIMITS[name]
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
            raise ValueError(f"limits.{name} must be an integer")
        if value < 1 or value > hard:
            raise ValueError(f"limits.{name} must be between 1 and {hard}")
        limits[name] = int(value)
    return limits


def request_limits(request: Any) -> dict[str, int]:
    """Return validated hard-capped request limits before loading external assets."""
    if not isinstance(request, Mapping):
        raise ValueError("request must be an object")
    return _request_limits(request.get("limits"))


def _finite_sum(values, name: str) -> float:
    try:
        result = math.fsum(values)
    except OverflowError as exc:
        raise ValueError(f"{name} sum overflowed") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} sum must be finite")
    return result


def _budget_result(limits: Mapping[str, int], sample_count: int, wavelength_count: int,
                   surface_count: int, layer_count: int, detector_pixels: int | None):
    path_count = sample_count * wavelength_count
    interactions = path_count * surface_count
    detector_cells = (detector_pixels or 0) * wavelength_count
    if path_count > limits["max_paths"]:
        raise ValueError("request exceeds limits.max_paths")
    if interactions > limits["max_interactions"]:
        raise ValueError("request exceeds limits.max_interactions")
    if detector_cells > limits["max_detector_cells"]:
        raise ValueError("request exceeds limits.max_detector_cells")
    estimated_bytes = path_count * (512 + surface_count * 768) + detector_cells * 32
    if estimated_bytes > limits["max_result_bytes"]:
        raise ValueError("request exceeds limits.max_result_bytes")
    if sample_count > limits["max_samples"]:
        raise ValueError("request exceeds limits.max_samples")
    if wavelength_count > limits["max_wavelengths"]:
        raise ValueError("request exceeds limits.max_wavelengths")
    if surface_count > limits["max_surfaces"]:
        raise ValueError("request exceeds limits.max_surfaces")
    if layer_count > limits["max_layers"]:
        raise ValueError("request exceeds limits.max_layers")
    return {"paths": path_count, "interactions": interactions,
            "detector_cells": detector_cells, "estimated_result_bytes": estimated_bytes}


def _axial_envelope(surface: Mapping[str, Any]) -> tuple[float, float]:
    geometry = surface["geometry"]
    if geometry["type"] == "plane":
        z = geometry["z_mm"]
        return z, z
    radius = geometry["radius_mm"]
    aperture = surface["aperture_radius_mm"]
    edge_sag = math.sqrt(max(0.0, radius * radius - aperture * aperture))
    center = geometry["center_z_mm"]
    if geometry["branch"] == "front":
        return center - radius, center - edge_sag
    return center + edge_sag, center + radius


def _read_request(request: Any):
    root = _object(request, "request", {"schema", "schema_version", "units", "polarization",
                                               "samples", "spectrum", "surfaces"}, {"detector", "limits"})
    if (root["schema"] != SCHEMA or isinstance(root["schema_version"], bool) or
            not isinstance(root["schema_version"], int) or root["schema_version"] != SCHEMA_VERSION):
        raise ValueError(f"request must use {SCHEMA!r} schema version {SCHEMA_VERSION}")
    units = _object(root["units"], "units", {"length", "wavelength", "index"})
    if units != {"length": "mm", "wavelength": "nm", "index": "n+i*k"}:
        raise ValueError("units must be length=mm, wavelength=nm, index=n+i*k")
    polarization = root["polarization"]
    if polarization not in ("s", "p", "unpolarized"):
        raise ValueError("polarization must be 's', 'p' or 'unpolarized' in the common XZ plane")
    limits = _request_limits(root.get("limits"))

    source_values = root["samples"]
    if not isinstance(source_values, list) or not source_values:
        raise ValueError("samples must be a non-empty array")
    if len(source_values) > limits["max_samples"]:
        raise ValueError("request exceeds limits.max_samples")
    samples = []
    sample_ids = set()
    sample_weights = []
    for i, raw in enumerate(source_values):
        name = f"samples[{i}]"
        item = _object(raw, name, {"sample_id", "origin_xz_mm", "direction_xz", "weight"})
        sample_id = _unique_string(item["sample_id"], f"{name}.sample_id")
        if sample_id in sample_ids:
            raise ValueError(f"duplicate sample_id {sample_id!r}")
        sample_ids.add(sample_id)
        origin = _pair(item["origin_xz_mm"], f"{name}.origin_xz_mm")
        direction = _pair(item["direction_xz"], f"{name}.direction_xz")
        norm = math.hypot(*direction)
        if abs(norm - 1.0) > 1e-9:
            raise ValueError(f"{name}.direction_xz must be normalized")
        if direction[1] <= 0:
            raise ValueError(f"{name}.direction_xz must travel forward with dz > 0")
        weight = _number(item["weight"], f"{name}.weight", minimum=0)
        sample_weights.append(weight)
        samples.append({"sample_id": sample_id, "origin": origin, "direction": direction,
                        "weight": weight})
    total_sample_weight = _finite_sum(sample_weights, "sample weights")
    if total_sample_weight <= 0:
        raise ValueError("sample weights must have a positive sum")

    spectrum_values = root["spectrum"]
    if not isinstance(spectrum_values, list) or not spectrum_values:
        raise ValueError("spectrum must be a non-empty array")
    if len(spectrum_values) > limits["max_wavelengths"]:
        raise ValueError("request exceeds limits.max_wavelengths")
    spectrum = []
    wave_values = set()
    spectrum_weights = []
    for i, raw in enumerate(spectrum_values):
        name = f"spectrum[{i}]"
        item = _object(raw, name, {"wavelength_nm", "weight"})
        wave = _number(item["wavelength_nm"], f"{name}.wavelength_nm", minimum=np.nextafter(0.0, 1.0))
        if wave in wave_values:
            raise ValueError(f"duplicate wavelength_nm {wave:g}")
        wave_values.add(wave)
        weight = _number(item["weight"], f"{name}.weight", minimum=0)
        spectrum_weights.append(weight)
        spectrum.append({"wavelength_nm": wave, "weight": weight})
    total_spectrum_weight = _finite_sum(spectrum_weights, "spectrum weights")
    if total_spectrum_weight <= 0:
        raise ValueError("spectrum weights must have a positive sum")

    surface_values = root["surfaces"]
    if not isinstance(surface_values, list):
        raise ValueError("surfaces must be an array")
    if len(surface_values) > limits["max_surfaces"]:
        raise ValueError("request exceeds limits.max_surfaces")
    raw_layer_count = 0
    for raw_surface in surface_values:
        if isinstance(raw_surface, Mapping):
            raw_coating = raw_surface.get("coating")
            if isinstance(raw_coating, Mapping) and isinstance(raw_coating.get("layers"), list):
                raw_layer_count += len(raw_coating["layers"])
    if raw_layer_count > limits["max_layers"]:
        raise ValueError("request exceeds limits.max_layers")
    surfaces = []
    surface_ids = set()
    layer_ids = set()
    for i, raw in enumerate(surface_values):
        name = f"surfaces[{i}]"
        item = _object(raw, name, {"surface_id", "geometry", "aperture_radius_mm",
                                  "medium_before", "medium_after"}, {"coating"})
        surface_id = _unique_string(item["surface_id"], f"{name}.surface_id")
        if surface_id in surface_ids:
            raise ValueError(f"duplicate surface_id {surface_id!r}")
        surface_ids.add(surface_id)
        geometry = _object(item["geometry"], f"{name}.geometry", {"type"},
                           {"z_mm", "center_z_mm", "radius_mm", "branch"})
        geometry_type = geometry["type"]
        if geometry_type == "plane":
            if set(geometry) != {"type", "z_mm"}:
                raise ValueError(f"{name}.geometry for a plane requires only z_mm")
            geometry = {"type": "plane", "z_mm": _number(geometry["z_mm"], f"{name}.geometry.z_mm")}
        elif geometry_type == "sphere":
            if set(geometry) != {"type", "center_z_mm", "radius_mm", "branch"}:
                raise ValueError(f"{name}.geometry for a sphere requires center_z_mm, radius_mm and branch")
            branch = geometry["branch"]
            if branch not in ("front", "back"):
                raise ValueError(f"{name}.geometry.branch must be 'front' or 'back'")
            geometry = {"type": "sphere",
                        "center_z_mm": _number(geometry["center_z_mm"], f"{name}.geometry.center_z_mm"),
                        "radius_mm": _number(geometry["radius_mm"], f"{name}.geometry.radius_mm",
                                             minimum=np.nextafter(0.0, 1.0)),
                        "branch": branch}
        else:
            raise ValueError(f"{name}.geometry.type must be 'plane' or 'sphere'")
        aperture = _number(item["aperture_radius_mm"], f"{name}.aperture_radius_mm",
                           minimum=np.nextafter(0.0, 1.0))
        if geometry_type == "sphere" and aperture >= geometry["radius_mm"]:
            raise ValueError(f"{name}.aperture_radius_mm must be smaller than its sphere radius")
        before = _index_rows(item["medium_before"], len(spectrum), f"{name}.medium_before", transparent=True)
        after = _index_rows(item["medium_after"], len(spectrum), f"{name}.medium_after", transparent=True)

        coating_layers = []
        stack_by_wave = [[before[wi], after[wi]] for wi in range(len(spectrum))]
        if "coating" in item:
            coating = _object(item["coating"], f"{name}.coating", {"indices", "layers"})
            if not isinstance(coating["layers"], list):
                raise ValueError(f"{name}.coating.layers must be an array")
            indices_rows = coating["indices"]
            if not isinstance(indices_rows, list) or len(indices_rows) != len(spectrum):
                raise ValueError(f"{name}.coating.indices must contain one incident-to-exit row per spectrum entry")
            stack_by_wave = []
            for wi, row in enumerate(indices_rows):
                if not isinstance(row, list) or len(row) != len(coating["layers"]) + 2:
                    raise ValueError(f"{name}.coating.indices[{wi}] must include both ports and one index per coating layer")
                parsed_row = [_index(value, f"{name}.coating.indices[{wi}][{mi}]")
                              for mi, value in enumerate(row)]
                if parsed_row[0].imag != 0 or parsed_row[-1].imag != 0:
                    raise ValueError(f"{name}.coating.indices[{wi}] incident and exit ports must be transparent")
                if abs(parsed_row[0] - before[wi]) > 1e-12:
                    raise ValueError(f"{name}.coating.indices[{wi}] incident port does not match medium_before")
                if abs(parsed_row[-1] - after[wi]) > 1e-12:
                    raise ValueError(f"{name}.coating.indices[{wi}] exit port does not match medium_after")
                stack_by_wave.append(parsed_row)
            for j, layer_raw in enumerate(coating["layers"]):
                layer_name = f"{name}.coating.layers[{j}]"
                layer = _object(layer_raw, layer_name, {"layer_id"},
                                {"thickness_nm", "deposition_step_id", "global_to_grid_xy"})
                layer_id = _unique_string(layer["layer_id"], f"{layer_name}.layer_id")
                if layer_id in layer_ids:
                    raise ValueError(f"duplicate layer_id {layer_id!r}")
                layer_ids.add(layer_id)
                has_uniform = "thickness_nm" in layer
                has_field = "deposition_step_id" in layer or "global_to_grid_xy" in layer
                if has_uniform == has_field:
                    raise ValueError(f"{layer_name} must define either uniform thickness_nm or a field step and transform")
                if has_uniform:
                    if set(layer) != {"layer_id", "thickness_nm"}:
                        raise ValueError(f"{layer_name} uniform layer has unsupported fields")
                    thickness = _number(layer["thickness_nm"], f"{layer_name}.thickness_nm", minimum=0)
                    coating_layers.append({"layer_id": layer_id, "thickness_nm": thickness})
                else:
                    if set(layer) != {"layer_id", "deposition_step_id", "global_to_grid_xy"}:
                        raise ValueError(f"{layer_name} field layer requires deposition_step_id and global_to_grid_xy")
                    step_id = _unique_string(layer["deposition_step_id"], f"{layer_name}.deposition_step_id")
                    transform = _object(layer["global_to_grid_xy"], f"{layer_name}.global_to_grid_xy",
                                        {"rotation_rad", "translation_xy_mm"})
                    rotation = _number(transform["rotation_rad"], f"{layer_name}.global_to_grid_xy.rotation_rad")
                    translation = _pair(transform["translation_xy_mm"], f"{layer_name}.global_to_grid_xy.translation_xy_mm")
                    coating_layers.append({"layer_id": layer_id, "deposition_step_id": step_id,
                                          "rotation_rad": rotation, "translation": translation})
        for wi, (n_before, n_after) in enumerate(zip(before, after)):
            if i > 0:
                previous = surfaces[-1]["after"][wi]
                if abs(previous - n_before) > 1e-12:
                    raise ValueError(f"{name}.medium_before does not match the previous surface's medium_after at spectrum[{wi}]")
        surfaces.append({"surface_id": surface_id, "geometry": geometry,
                         "aperture_radius_mm": aperture, "before": before,
                         "after": after, "layers": coating_layers,
                         "stack_by_wave": stack_by_wave})

    for previous, current in zip(surfaces, surfaces[1:]):
        _previous_min, previous_max = _axial_envelope(previous)
        current_min, _current_max = _axial_envelope(current)
        if current_min <= previous_max + _SURFACE_SPACING_TOL_MM:
            raise ValueError(
                "successive surface axial envelopes must have positive spacing; "
                "intersecting or near-coincident plane/sphere pairs are unsupported")

    detector = None
    if "detector" in root:
        raw_detector = _object(root["detector"], "detector",
                               {"z_mm", "aperture_radius_mm", "pixel_count"})
        pixel_count = raw_detector["pixel_count"]
        if isinstance(pixel_count, (bool, np.bool_)) or not isinstance(pixel_count, (int, np.integer)):
            raise ValueError("detector.pixel_count must be a positive integer")
        if pixel_count < 1:
            raise ValueError("detector.pixel_count must be a positive integer")
        detector = {"z_mm": _number(raw_detector["z_mm"], "detector.z_mm"),
                    "aperture_radius_mm": _number(raw_detector["aperture_radius_mm"],
                                                  "detector.aperture_radius_mm",
                                                  minimum=np.nextafter(0.0, 1.0)),
                    "pixel_count": int(pixel_count)}

    _budget_result(limits, len(samples), len(spectrum), len(surfaces),
                   sum(len(surface["layers"]) for surface in surfaces),
                   detector["pixel_count"] if detector else None)
    for sample in samples:
        for wave in spectrum:
            if not math.isfinite(sample["weight"] * wave["weight"]):
                raise ValueError("sample and spectrum weight product must be finite")

    return polarization, samples, spectrum, surfaces, detector, limits


def _grid_data(layer_fields: Any, step_id: str):
    if not isinstance(layer_fields, Mapping):
        raise ValueError("layer_fields must map deposition_step_id values to in-memory grid arrays")
    if step_id not in layer_fields:
        raise ValueError(f"layer_fields is missing deposition_step_id {step_id!r}; recompute with per-step output or use a uniform film")
    grid = _object(layer_fields[step_id], f"layer_fields[{step_id!r}]",
                   {"x_mm", "y_mm", "thickness_nm"}, {"valid_mask", "valid_cell_mask"})
    x = np.asarray(grid["x_mm"], dtype=float)
    y = np.asarray(grid["y_mm"], dtype=float)
    thickness = np.asarray(grid["thickness_nm"], dtype=float)
    if x.ndim != 1 or x.size < 2 or not np.all(np.isfinite(x)) or np.any(np.diff(x) <= 0):
        raise ValueError(f"layer_fields[{step_id!r}].x_mm must be finite, strictly increasing and contain at least two values")
    if y.ndim != 1 or y.size < 2 or not np.all(np.isfinite(y)) or np.any(np.diff(y) <= 0):
        raise ValueError(f"layer_fields[{step_id!r}].y_mm must be finite, strictly increasing and contain at least two values")
    if thickness.shape != (y.size, x.size):
        raise ValueError(f"layer_fields[{step_id!r}].thickness_nm must have [y,x] shape")
    if "valid_mask" not in grid:
        valid = np.ones(thickness.shape, dtype=bool)
        if not np.all(np.isfinite(thickness)) or np.any(thickness < 0):
            raise ValueError(f"layer_fields[{step_id!r}].thickness_nm must be a nonnegative finite [y,x] grid")
    else:
        raw_mask = grid["valid_mask"]
        try:
            valid = np.asarray(raw_mask)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"layer_fields[{step_id!r}].valid_mask must be a boolean [y,x] grid") from exc
        if valid.dtype.kind != "b" or valid.ndim != 2 or valid.shape != thickness.shape:
            raise ValueError(f"layer_fields[{step_id!r}].valid_mask must be a boolean [y,x] grid")
        if np.isinf(thickness).any() or np.any(thickness[np.isfinite(thickness)] < 0):
            raise ValueError(f"layer_fields[{step_id!r}].thickness_nm must not contain infinity or negative values")
        if not np.all(np.isfinite(thickness[valid])):
            raise ValueError(f"layer_fields[{step_id!r}].thickness_nm must be finite inside valid_mask")
    cell_shape = (y.size - 1, x.size - 1)
    if "valid_cell_mask" not in grid:
        valid_cells = np.ones(cell_shape, dtype=bool)
    else:
        try:
            valid_cells = np.asarray(grid["valid_cell_mask"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"layer_fields[{step_id!r}].valid_cell_mask must be boolean [y-1,x-1]") from exc
        if valid_cells.dtype.kind != "b" or valid_cells.ndim != 2 or valid_cells.shape != cell_shape:
            raise ValueError(f"layer_fields[{step_id!r}].valid_cell_mask must be boolean [y-1,x-1]")
    return x, y, thickness, valid, valid_cells


def _sample_field(grid, x_grid: float, y_grid: float, step_id: str) -> float:
    x, y, values, valid, valid_cells = grid
    tol = 1e-12 * max(1.0, abs(x[0]), abs(x[-1]), abs(y[0]), abs(y[-1]))
    if x_grid < x[0] - tol or x_grid > x[-1] + tol or y_grid < y[0] - tol or y_grid > y[-1] + tol:
        raise _FieldOutsideGrid(step_id)
    x_grid = min(max(x_grid, x[0]), x[-1])
    y_grid = min(max(y_grid, y[0]), y[-1])
    ix = min(max(int(np.searchsorted(x, x_grid, side="right") - 1), 0), x.size - 2)
    iy = min(max(int(np.searchsorted(y, y_grid, side="right") - 1), 0), y.size - 2)
    # Conservative boundary rule: all four cell corners must be in-domain,
    # even when one corner's bilinear weight is exactly zero.
    if not np.all(valid[iy:iy + 2, ix:ix + 2]) or not valid_cells[iy, ix]:
        raise _FieldOutsideGrid(step_id)
    fx = (x_grid - x[ix]) / (x[ix + 1] - x[ix])
    fy = (y_grid - y[iy]) / (y[iy + 1] - y[iy])
    low = values[iy, ix] * (1 - fx) + values[iy, ix + 1] * fx
    high = values[iy + 1, ix] * (1 - fx) + values[iy + 1, ix + 1] * fx
    return float(low * (1 - fy) + high * fy)


class _FieldOutsideGrid(Exception):
    def __init__(self, step_id: str):
        self.step_id = step_id


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("cancelled")


def _check_finite_tree(value, name="result"):
    if isinstance(value, Mapping):
        for key, item in value.items():
            _check_finite_tree(item, f"{name}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_finite_tree(item, f"{name}[{index}]")
    elif isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, (bool, np.bool_)):
        if not math.isfinite(float(value)):
            raise ValueError(f"{name} contains a non-finite numeric value")


def _intersect(ray_origin, ray_direction, geometry):
    ox, oz = ray_origin
    dx, dz = ray_direction
    if geometry["type"] == "plane":
        t = (geometry["z_mm"] - oz) / dz
        if t <= 1e-12:
            return None
        x = ox + t * dx
        z = oz + t * dz
        return (x, z), (0.0, 1.0), t

    center_z = geometry["center_z_mm"]
    radius = geometry["radius_mm"]
    rz = oz - center_z
    b = ox * dx + rz * dz
    c = ox * ox + rz * rz - radius * radius
    discriminant = b * b - c
    if discriminant < 0:
        return None
    root = math.sqrt(max(0.0, discriminant))
    candidates = sorted(t for t in (-b - root, -b + root) if t > 1e-12)
    sign = -1.0 if geometry["branch"] == "front" else 1.0
    for t in candidates:
        x = ox + t * dx
        z = oz + t * dz
        if sign * (z - center_z) < -1e-10:
            continue
        radial = np.array([x / radius, (z - center_z) / radius], dtype=float)
        normal = -radial if geometry["branch"] == "front" else radial
        norm = float(np.linalg.norm(normal))
        if norm == 0:
            continue
        normal /= norm
        return (x, z), (float(normal[0]), float(normal[1])), t
    return None


def _new_direction(old, normal, n_before: float, n_after: float):
    cosine_i = float(np.dot(old, normal))
    if cosine_i <= 0:
        return None
    tangent = np.asarray(old, dtype=float) - cosine_i * np.asarray(normal, dtype=float)
    sine_i = float(np.linalg.norm(tangent))
    sine_t = (n_before / n_after) * sine_i
    if sine_t >= 1.0:
        return None
    if sine_i > 0:
        tangent *= sine_t / sine_i
    result = tangent + math.sqrt(max(0.0, 1.0 - sine_t * sine_t)) * np.asarray(normal)
    norm = float(np.linalg.norm(result))
    result /= norm
    if result[1] <= 0:
        return None
    return float(result[0]), float(result[1])


def _rta(wave, stack, thicknesses, angle_deg, polarization):
    result = coherent_rta(wave, stack, thicknesses, angle_deg=angle_deg,
                          polarization=polarization)
    r, t, a = (float(result[key]) for key in ("R", "T", "A"))
    if not all(math.isfinite(item) for item in (r, t, a)):
        raise ValueError("film solver returned a non-finite R/T/A value")
    if min(r, t, a) < -1e-9 or max(r, t, a) > 1 + 1e-9 or abs(r + t + a - 1.0) > 1e-8:
        raise ValueError("film solver returned R/T/A outside passive energy bounds")
    return {"R": r, "T": t, "A": a}


def trace(request: Mapping[str, Any], *, layer_fields: Mapping[str, Any] | None = None,
          cancel_event: Any = None, progress: Any = None) -> dict[str, Any]:
    """Trace weighted forward rays through versioned plane/spherical interfaces.

    `request` is JSON-compatible version 1 data. Optional deposition thickness
    grids are passed separately via `layer_fields`, keyed by stable
    `deposition_step_id`; each grid is bilinearly sampled at the wavelength-
    dependent surface hit after applying its optical-global XY-to-grid rigid
    transform. Out-of-grid hits terminate that ray and are reported as missed
    power. Missing grid assets and malformed numerical inputs raise ValueError.

    Source and spectrum weights are explicit relative energy weights and are
    not normalized. Result energy fields are in their product units. The
    spectrum weight is applied to each per-wavelength energy total.

    Optional cancellation uses an event-like object exposing `is_set()` and
    raises `InterruptedError("cancelled")` at path and surface boundaries.
    Optional `progress` is a callback receiving mappings with
    `completed_fraction` and `stage` (`tracing` or `complete`).
    """
    if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
        raise ValueError("cancel_event must expose a callable is_set() method")
    if progress is not None and not callable(progress):
        raise ValueError("progress must be callable")
    polarization, samples, spectrum, surfaces, detector, limits = _read_request(request)

    grids = {}
    referenced_steps = {layer["deposition_step_id"] for surface in surfaces
                        for layer in surface["layers"] if "deposition_step_id" in layer}
    if referenced_steps:
        if not isinstance(layer_fields, Mapping):
            raise ValueError("layer_fields must be supplied for every deposition-backed coating layer")
        for step_id in referenced_steps:
            grids[step_id] = _grid_data(layer_fields, step_id)
        extra_steps = set(layer_fields) - referenced_steps
        if extra_steps:
            raise ValueError(f"layer_fields contains unreferenced step ID(s): {', '.join(sorted(extra_steps))}")
    elif layer_fields is not None:
        if not isinstance(layer_fields, Mapping):
            raise ValueError("layer_fields must be a mapping")
        extra_steps = set(layer_fields)
        if extra_steps:
            raise ValueError("layer_fields contains step IDs not referenced by the request")

    paths = []
    by_wave = [{"wavelength_nm": item["wavelength_nm"], "spectrum_weight": item["weight"],
                "incident_weight": 0.0, "transmitted_weight": 0.0,
                "reflected_weight": 0.0, "absorbed_weight": 0.0,
                "missed_weight": 0.0} for item in spectrum]
    detector_spectra = None
    detector_edges = None
    if detector is not None:
        detector_edges = np.linspace(-detector["aperture_radius_mm"],
                                     detector["aperture_radius_mm"],
                                     detector["pixel_count"] + 1)
        detector_spectra = [{"wavelength_nm": item["wavelength_nm"],
                             "spectrum_weight": item["weight"],
                             "detected_weight": 0.0, "missed_weight": 0.0,
                             "ray_energy_histogram_weight": [0.0] * detector["pixel_count"]}
                            for item in spectrum]

    total_paths = len(spectrum) * len(samples)
    completed_paths = 0
    if progress is not None:
        progress({"completed_fraction": 0.0, "stage": "tracing"})
    for wi, wave in enumerate(spectrum):
        for sample in samples:
            _check_cancel(cancel_event)
            base_power = sample["weight"] * wave["weight"]
            if not math.isfinite(base_power):
                raise ValueError("sample and spectrum weight product must be finite")
            by_wave[wi]["incident_weight"] += base_power
            if polarization == "unpolarized":
                components = {"s": base_power * 0.5, "p": base_power * 0.5}
            else:
                components = {polarization: base_power}
            ray_origin = sample["origin"]
            ray_direction = sample["direction"]
            events = []
            cumulative_reflected = 0.0
            cumulative_absorbed = 0.0
            cumulative_missed = 0.0
            status = "transmitted"

            for surface in surfaces:
                _check_cancel(cancel_event)
                incoming = sum(components.values())
                intersection = _intersect(ray_origin, ray_direction, surface["geometry"])
                if intersection is None:
                    cumulative_missed += incoming
                    components = {key: 0.0 for key in components}
                    status = "no_forward_intersection"
                    by_wave[wi]["missed_weight"] += incoming
                    break
                hit, normal, _ = intersection
                if abs(hit[0]) > surface["aperture_radius_mm"] + 1e-12:
                    cumulative_missed += incoming
                    components = {key: 0.0 for key in components}
                    status = "outside_aperture"
                    by_wave[wi]["missed_weight"] += incoming
                    break
                cosine_i = float(np.dot(ray_direction, normal))
                if cosine_i <= 0:
                    cumulative_missed += incoming
                    components = {key: 0.0 for key in components}
                    status = "wrong_incident_side"
                    by_wave[wi]["missed_weight"] += incoming
                    break
                angle_deg = math.degrees(math.acos(min(1.0, max(-1.0, cosine_i))))
                sampled_thicknesses = []
                hit_grid_xy = None
                try:
                    for layer in surface["layers"]:
                        if "thickness_nm" in layer:
                            thickness = layer["thickness_nm"]
                        else:
                            c = math.cos(layer["rotation_rad"])
                            s = math.sin(layer["rotation_rad"])
                            gx = c * hit[0] - s * 0.0 + layer["translation"][0]
                            gy = s * hit[0] + c * 0.0 + layer["translation"][1]
                            hit_grid_xy = [gx, gy]
                            thickness = _sample_field(grids[layer["deposition_step_id"]], gx, gy,
                                                      layer["deposition_step_id"])
                        sampled_thicknesses.append(thickness)
                except _FieldOutsideGrid as exc:
                    cumulative_missed += incoming
                    components = {key: 0.0 for key in components}
                    status = "outside_thickness_field"
                    by_wave[wi]["missed_weight"] += incoming
                    events.append({"surface_id": surface["surface_id"],
                                   "hit_xz_mm": list(hit), "hit_global_xy_mm": [hit[0], 0.0],
                                   "grid_xy_mm": hit_grid_xy,
                                   "incidence_angle_deg": angle_deg,
                                   "sampled_layer_thickness_nm": sampled_thicknesses,
                                   "terminal_step_id": exc.step_id,
                                   "input_weight": incoming,
                                   "reflected_weight": 0.0, "absorbed_weight": 0.0,
                                   "transmitted_weight": 0.0})
                    break

                stack = surface["stack_by_wave"][wi]
                component_rta = {pol: _rta(wave["wavelength_nm"], stack, sampled_thicknesses,
                                            angle_deg, pol) for pol in components}
                reflected = sum(power * component_rta[pol]["R"] for pol, power in components.items())
                absorbed = sum(power * component_rta[pol]["A"] for pol, power in components.items())
                transmitted = sum(power * component_rta[pol]["T"] for pol, power in components.items())
                if incoming > 0:
                    effective = {key: amount / incoming for key, amount in
                                 (("R", reflected), ("T", transmitted), ("A", absorbed))}
                else:
                    # Event R/T/A are dimensionless coefficients. A zero-energy
                    # ray has no power-weighted polarization mix, so fall back to
                    # the request's defined input polarization state.
                    fractions = ({"s": 0.5, "p": 0.5} if polarization == "unpolarized"
                                 else {polarization: 1.0})
                    effective = {key: sum(fraction * component_rta[pol][key]
                                          for pol, fraction in fractions.items())
                                 for key in ("R", "T", "A")}
                cumulative_reflected += reflected
                cumulative_absorbed += absorbed
                by_wave[wi]["reflected_weight"] += reflected
                by_wave[wi]["absorbed_weight"] += absorbed
                events.append({"surface_id": surface["surface_id"],
                               "hit_xz_mm": list(hit),
                               "hit_global_xy_mm": [hit[0], 0.0],
                               "grid_xy_mm": hit_grid_xy,
                               "normal_xz": list(normal),
                               "incidence_angle_deg": angle_deg,
                               "sampled_layer_thickness_nm": sampled_thicknesses,
                               "rta": effective,
                               "rta_by_polarization": component_rta,
                               "input_weight": incoming,
                               "reflected_weight": reflected,
                               "absorbed_weight": absorbed,
                               "transmitted_weight": transmitted})

                outgoing = _new_direction(ray_direction, normal,
                                          surface["before"][wi].real,
                                          surface["after"][wi].real)
                if outgoing is None:
                    if transmitted > 0:
                        # A grazing/non-forward Snell solution is not traced
                        # further; preserve accounting by marking it missed.
                        cumulative_missed += transmitted
                        by_wave[wi]["missed_weight"] += transmitted
                    components = {key: 0.0 for key in components}
                    status = ("total_internal_reflection" if transmitted <= 1e-14 * max(1.0, incoming)
                              else "nonforward_transmission")
                    break
                if transmitted <= 0:
                    components = {key: 0.0 for key in components}
                    status = "no_transmitted_power"
                    break
                for pol in components:
                    components[pol] *= component_rta[pol]["T"]
                ray_origin = hit
                ray_direction = outgoing

            final_power = sum(components.values())
            by_wave[wi]["transmitted_weight"] += final_power
            path = {"sample_id": sample["sample_id"],
                    "wavelength_nm": wave["wavelength_nm"],
                    "sample_weight": sample["weight"],
                    "spectrum_weight": wave["weight"],
                    "status": status,
                    "events": events,
                    "final_ray": ({"origin_xz_mm": list(ray_origin),
                                   "direction_xz": list(ray_direction)}
                                  if status == "transmitted" else None),
                    "powers": {"incident_weight": base_power,
                               "transmitted_weight": final_power,
                               "reflected_weight": cumulative_reflected,
                               "absorbed_weight": cumulative_absorbed,
                               "missed_weight": cumulative_missed}}
            if detector is not None:
                detector_x = None
                detector_power = 0.0
                if status != "transmitted":
                    detector_status = "no_transmitted_ray"
                    detector_captured = False
                else:
                    dz_to_detector = detector["z_mm"] - ray_origin[1]
                    if dz_to_detector <= 1e-12:
                        detector_status = "plane_not_forward"
                        detector_captured = False
                    else:
                        distance = dz_to_detector / ray_direction[1]
                        detector_x = ray_origin[0] + distance * ray_direction[0]
                        if abs(detector_x) > detector["aperture_radius_mm"] + 1e-12:
                            detector_status = "outside_aperture"
                            detector_captured = False
                        else:
                            detector_status = "detected"
                            detector_captured = True
                            detector_power = final_power
                            clamped_x = min(max(detector_x, -detector["aperture_radius_mm"]),
                                            detector["aperture_radius_mm"])
                            bin_index = int(np.searchsorted(detector_edges, clamped_x,
                                                            side="right") - 1)
                            bin_index = min(max(bin_index, 0), detector["pixel_count"] - 1)
                            detector_spectra[wi]["ray_energy_histogram_weight"][bin_index] += detector_power
                            detector_spectra[wi]["detected_weight"] += detector_power
                    if not detector_captured:
                        detector_spectra[wi]["missed_weight"] += final_power
                path["detector_hit_x_mm"] = detector_x
                path["detector_status"] = detector_status
                path["detector_captured"] = detector_captured
                path["detector_weight"] = detector_power
            paths.append(path)
            completed_paths += 1
            if progress is not None and (
                    completed_paths % limits["batch_paths"] == 0 or completed_paths == total_paths):
                progress({"completed_fraction": completed_paths / total_paths,
                          "stage": "tracing"})

    _check_finite_tree({"spectra": by_wave, "paths": paths,
                        "detector": detector_spectra, "detector_edges": detector_edges})
    for totals in by_wave:
        incident = totals["incident_weight"]
        accounted = sum(totals[key] for key in ("transmitted_weight", "reflected_weight",
                                                  "absorbed_weight", "missed_weight"))
        if abs(incident - accounted) > 1e-8 * max(1.0, incident):
            raise ValueError("internal energy accounting failed for a wavelength")
        totals["transmittance"] = totals["transmitted_weight"] / incident if incident else 0.0
        totals["reflectance"] = totals["reflected_weight"] / incident if incident else 0.0
        totals["absorptance"] = totals["absorbed_weight"] / incident if incident else 0.0
        totals["missed_fraction"] = totals["missed_weight"] / incident if incident else 0.0

    result = {"schema": RESULT_SCHEMA, "schema_version": SCHEMA_VERSION,
              "units": {"length": "mm", "wavelength": "nm", "index": "n+i*k"},
              "polarization": polarization,
              "model": "ordered_meridional_geometric_rays_local_coherent_coatings",
              "phase_between_surfaces": "not modeled",
              "spectra": by_wave,
              "paths": paths}
    if detector is not None:
        for totals in detector_spectra:
            binned = sum(totals["ray_energy_histogram_weight"])
            if abs(binned - totals["detected_weight"]) > 1e-10 * max(1.0, totals["detected_weight"]):
                raise ValueError("internal detector histogram accounting failed")
            optical_transmitted = next(item["transmitted_weight"] for item in by_wave
                                       if item["wavelength_nm"] == totals["wavelength_nm"])
            if abs(optical_transmitted - totals["detected_weight"] - totals["missed_weight"]) > 1e-8 * max(1.0, optical_transmitted):
                raise ValueError("internal detector capture accounting failed")
        result["detector"] = {"z_mm": detector["z_mm"],
                              "aperture_radius_mm": detector["aperture_radius_mm"],
                              "pixel_count": detector["pixel_count"],
                              "x_bin_edges_mm": detector_edges.tolist(),
                              "spectra": detector_spectra,
                              "interpretation": "geometric ray-energy histogram; not a diffraction PSF"}
    _check_finite_tree(result)
    if progress is not None:
        progress({"completed_fraction": 1.0, "stage": "complete"})
    return result

