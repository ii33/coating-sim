"""Strict input contract for the optically explicit appearance prototype."""

from __future__ import annotations

from collections.abc import Mapping
import math

import numpy as np


class AppearanceInputError(ValueError):
    """Raised when an appearance job omits physical data or exceeds its domain."""


def _finite(value, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise AppearanceInputError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AppearanceInputError(f"{label} must be a finite number") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise AppearanceInputError(f"{label} must be finite" + (" and positive" if positive else ""))
    return result


def _vec(value, label: str, size: int = 3) -> np.ndarray:
    result = np.asarray(value, dtype=np.float64)
    if result.shape != (size,) or not np.isfinite(result).all():
        raise AppearanceInputError(f"{label} must contain {size} finite values")
    return result


def _material(raw: Mapping, label: str, wavelengths: np.ndarray, *, transparent: bool = False) -> dict:
    if not isinstance(raw, Mapping):
        raise AppearanceInputError(f"{label} must be an explicit material object")
    if "n" not in raw:
        raise AppearanceInputError(f"{label}.n is required")

    def material_values(key: str, *, default_zero=False) -> np.ndarray:
        if default_zero:
            return np.zeros(wavelengths.shape, dtype=np.float64)
        raw_values = raw.get(key)
        if isinstance(raw_values, (list, tuple)):
            sample_waves = np.asarray(raw.get("wavelength_nm"), dtype=np.float64)
            values = np.asarray(raw_values, dtype=np.float64)
            if (sample_waves.ndim != 1 or values.shape != sample_waves.shape or
                    sample_waves.size < 2 or not np.isfinite(sample_waves).all() or
                    not np.isfinite(values).all() or np.any(np.diff(sample_waves) <= 0)):
                raise AppearanceInputError(f"{label}.{key} arrays need matching ordered wavelength_nm samples")
            if wavelengths.min() < sample_waves[0] or wavelengths.max() > sample_waves[-1]:
                raise AppearanceInputError(f"wavelength sampling exceeds {label}.{key} data; extrapolation is disabled")
            return np.interp(wavelengths, sample_waves, values)
        return np.full(wavelengths.shape, _finite(raw_values, f"{label}.{key}", positive=(key == "n")))

    n_values = material_values("n")
    if np.any(n_values <= 0):
        raise AppearanceInputError(f"{label}.n must be positive")
    if "k" not in raw:
        if raw.get("assume_lossless") is not True:
            raise AppearanceInputError(f"{label}.k is unknown; provide k or explicitly set assume_lossless=true")
        k_values = material_values("k", default_zero=True)
    else:
        k_values = material_values("k")
        if np.any(k_values < 0):
            raise AppearanceInputError(f"{label}.k must be nonnegative for a passive material")
    band = raw.get("valid_wavelength_nm")
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        raise AppearanceInputError(f"{label}.valid_wavelength_nm must declare the supported band")
    lo, hi = (_finite(band[0], f"{label}.valid_wavelength_nm[0]"),
              _finite(band[1], f"{label}.valid_wavelength_nm[1]"))
    if lo >= hi or wavelengths.min() < lo or wavelengths.max() > hi:
        raise AppearanceInputError(f"wavelength sampling exceeds {label}'s declared data band")
    if transparent and np.any(k_values != 0):
        raise AppearanceInputError(f"{label} must be transparent in this slab prototype")
    return {"n_values": n_values, "k_values": k_values, "valid_wavelength_nm": [lo, hi],
            "assumed_lossless": "k" not in raw or raw.get("assume_lossless") is True}


def _face_layers(raw_layers, face: str, waves: np.ndarray, bounds: np.ndarray,
                 *, circle_radius_mm: float | None = None) -> list[dict]:
    if not isinstance(raw_layers, list):
        raise AppearanceInputError(f"{face}_layers must be an explicit list")
    normalized = []
    for index, layer in enumerate(raw_layers):
        if not isinstance(layer, Mapping) or not isinstance(layer.get("layer_id"), str):
            raise AppearanceInputError(f"{face}_layers[{index}] must identify a frozen film layer")
        field = layer.get("thickness_field")
        if not isinstance(field, Mapping) or field.get("unit") != "nm" or field.get("origin") != "captured_process_step":
            raise AppearanceInputError(f"{face} layer {layer['layer_id']} must use an actual captured process thickness field")
        x = np.asarray(field.get("x_mm"), dtype=np.float64)
        y = np.asarray(field.get("y_mm"), dtype=np.float64)
        d = np.asarray(field.get("thickness_nm"), dtype=np.float64)
        valid = np.asarray(field.get("valid_mask"), dtype=bool)
        cells = np.asarray(field.get("valid_cell_mask"), dtype=bool)
        if (x.ndim != 1 or y.ndim != 1 or x.size < 2 or y.size < 2 or
                np.any(np.diff(x) <= 0) or np.any(np.diff(y) <= 0) or
                d.shape != (y.size, x.size) or valid.shape != d.shape or
                cells.shape != (y.size - 1, x.size - 1) or
                not np.isfinite(x).all() or not np.isfinite(y).all() or
                not np.isfinite(d[valid]).all() or np.any(d[valid] < 0)):
            raise AppearanceInputError(f"captured thickness field for {layer['layer_id']} is invalid")
        if circle_radius_mm is None:
            if not np.all(valid) or not np.all(cells):
                raise AppearanceInputError("missing thickness cells inside the rectangular substrate are not renderable")
        else:
            from .circle import circle_node_mask
            domain = circle_node_mask(x, y, circle_radius_mm)
            if not np.array_equal(valid, domain):
                raise AppearanceInputError("circle captured validity mask must match the whole circle; missing interior nodes are not renderable")
            expected_cells = valid[:-1, :-1] & valid[:-1, 1:] & valid[1:, :-1] & valid[1:, 1:]
            if not np.array_equal(cells, expected_cells):
                raise AppearanceInputError("circle captured valid_cell_mask must equal its four-corner validity conjunction")
        if not (math.isclose(x[0], bounds[0]) and math.isclose(x[-1], bounds[1]) and
                math.isclose(y[0], bounds[2]) and math.isclose(y[-1], bounds[3])):
            raise AppearanceInputError("captured field extent must exactly cover the supported rectangular substrate")
        normalized.append({"layer_id": layer["layer_id"],
                           "material": _material(layer.get("material"), f"layer {layer['layer_id']}.material", waves),
                           "x_mm": x, "y_mm": y, "thickness_nm": d, "valid_mask": valid,
                           **({"valid_cell_mask": cells} if circle_radius_mm is not None else {})})
    if normalized:
        x, y = normalized[0]["x_mm"], normalized[0]["y_mm"]
        if any(not np.array_equal(x, item["x_mm"]) or not np.array_equal(y, item["y_mm"])
               for item in normalized[1:]):
            raise AppearanceInputError(f"all {face} layers must share the captured process grid")
    return normalized


def _spectrum(raw: Mapping, label: str, wavelengths: np.ndarray, *, allow_relative: bool = True) -> tuple[np.ndarray, dict]:
    if not isinstance(raw, Mapping):
        raise AppearanceInputError(f"{label} must be wavelength-indexed scalar data, not RGB")
    sample_wavelengths = np.asarray(raw.get("wavelength_nm"), dtype=np.float64)
    values = np.asarray(raw.get("value"), dtype=np.float64)
    if (sample_wavelengths.ndim != 1 or values.shape != sample_wavelengths.shape or
            sample_wavelengths.size < 2 or not np.isfinite(sample_wavelengths).all() or
            not np.isfinite(values).all() or np.any(np.diff(sample_wavelengths) <= 0) or
            np.any(values < 0)):
        raise AppearanceInputError(f"{label} needs at least two ordered, nonnegative scalar wavelength samples")
    if wavelengths.min() < sample_wavelengths[0] or wavelengths.max() > sample_wavelengths[-1]:
        raise AppearanceInputError(f"wavelength sampling exceeds {label}'s data; extrapolation is disabled")
    kind = raw.get("kind")
    if kind not in (("relative", "absolute") if allow_relative else ("absolute",)):
        raise AppearanceInputError(f"{label}.kind must be explicit ('relative' or 'absolute')")
    scale = _finite(raw.get("scale", 1.0), f"{label}.scale", positive=True)
    if kind == "absolute" and raw.get("calibration") != "absolute_radiometric":
        raise AppearanceInputError(f"{label} claims absolute units without an absolute_radiometric calibration")
    return np.interp(wavelengths, sample_wavelengths, values) * scale, {
        "kind": kind, "scale": scale, "calibration": raw.get("calibration", "uncalibrated"),
        "input_samples": int(sample_wavelengths.size),
    }


def validate_job(job: Mapping, *, max_pixels: int = 1_048_576,
                 max_wavelengths: int = 241, max_spp: int = 4096,
                 max_wavelength_samples: int | None = 20_000_000) -> dict:
    """Validate frozen inputs; None disables only the aggregate execution budget."""
    if max_wavelength_samples is not None and (type(max_wavelength_samples) is not int or max_wavelength_samples < 1):
        raise AppearanceInputError("max_wavelength_samples must be a positive integer or None")
    if not isinstance(job, Mapping) or job.get("schema_version") != 1:
        raise AppearanceInputError("appearance job must be an object with schema_version=1")
    if job.get("coordinate_unit") != "mm":
        raise AppearanceInputError("scene coordinates must declare process-local millimetres")
    waves = np.asarray(job.get("wavelength_nm"), dtype=np.float64)
    if (waves.ndim != 1 or waves.size < 2 or waves.size > max_wavelengths or
            not np.isfinite(waves).all() or np.any(waves <= 0) or np.any(np.diff(waves) <= 0)):
        raise AppearanceInputError(f"wavelength_nm must contain 2..{max_wavelengths} ordered positive samples")
    if waves.min() < 360.0 or waves.max() > 830.0:
        raise AppearanceInputError("XYZ output requires wavelengths within the CIE 1931 table domain [360,830] nm")

    substrate = job.get("substrate")
    if not isinstance(substrate, Mapping) or substrate.get("outline_kind") not in ("rectangle", "circle"):
        raise AppearanceInputError("prototype supports explicitly flat rectangle or circle substrate outlines only")
    outline_kind = substrate["outline_kind"]
    circle_radius = (_finite(substrate.get("radius_mm"), "substrate.radius_mm", positive=True)
                     if outline_kind == "circle" else None)
    bounds = np.asarray(substrate.get("bounds_mm"), dtype=np.float64)
    if bounds.shape != (4,) or not np.isfinite(bounds).all() or bounds[0] >= bounds[1] or bounds[2] >= bounds[3]:
        raise AppearanceInputError("substrate.bounds_mm must be [xmin,xmax,ymin,ymax] with positive area")
    if circle_radius is not None and not np.allclose(
            bounds, [-circle_radius, circle_radius, -circle_radius, circle_radius], rtol=1e-12, atol=0.0):
        raise AppearanceInputError("circle bounds_mm must be the origin-centred radius bounds [-R,R,-R,R]")
    solid_mm = _finite(substrate.get("solid_thickness_mm"), "substrate.solid_thickness_mm", positive=True)
    thickness_provenance = substrate.get("solid_thickness_provenance")
    if thickness_provenance not in ("explicit_geometry_input", "film_design_snapshot/design_value"):
        raise AppearanceInputError("substrate solid thickness needs an explicit geometry source")
    geometry_note = None
    if thickness_provenance == "film_design_snapshot/design_value":
        geometry_note = (
            f"design_value_not_measurement; {'circular' if circle_radius is not None else 'rectangular'} slab assumes constant thickness "
            "and a parallel flat back surface"
        )
    substrate_material = _material(substrate.get("material"), "substrate.material", waves, transparent=True)
    back = substrate.get("back_surface")
    if not isinstance(back, Mapping):
        raise AppearanceInputError("back_surface must reflect the actual frozen film-design snapshot")
    back = dict(back)
    raw_back_layers = back.get("layers", [])
    if not isinstance(raw_back_layers, list):
        raise AppearanceInputError("back_surface.layers must be an array from the frozen film-design snapshot")
    actual_back_state = "coated" if raw_back_layers else "uncoated"
    if back.get("status") not in (None, actual_back_state):
        raise AppearanceInputError("back-surface status conflicts with the frozen layer list")
    if back.get("outside_medium", "air") != "air":
        raise AppearanceInputError("prototype currently requires the back face's actual outside medium to be air")

    resolution = job.get("resolution")
    if not isinstance(resolution, Mapping):
        raise AppearanceInputError("resolution is required")
    integer_values = (resolution.get("width"), resolution.get("height"), resolution.get("spp"),
                      resolution.get("seed", 0))
    if any(type(value) is not int for value in integer_values):
        raise AppearanceInputError("resolution width, height, spp, and seed must be integers")
    width, height, spp, seed = integer_values
    if min(width, height, spp) < 1 or seed < 0 or seed > 0xFFFFFFFF or width * height > max_pixels or spp > max_spp:
        raise AppearanceInputError("resolution exceeds configured pixel/sample limits")
    if max_wavelength_samples is not None and width * height * spp * waves.size > max_wavelength_samples:
        budget_label = "20 million" if max_wavelength_samples == 20_000_000 else str(max_wavelength_samples)
        raise AppearanceInputError(f"estimated scalar wavelength sample count exceeds the {budget_label} render budget")

    camera = job.get("camera")
    if not isinstance(camera, Mapping):
        raise AppearanceInputError("camera pose is required")
    cam_pos, cam_target, cam_up = (_vec(camera.get(key), f"camera.{key}")
                                   for key in ("position_mm", "target_mm", "up"))
    if np.linalg.norm(cam_target - cam_pos) == 0 or np.linalg.norm(np.cross(cam_target - cam_pos, cam_up)) < 1e-9:
        raise AppearanceInputError("camera position, target, and up must define a nondegenerate pose")
    fov = _finite(camera.get("fov_y_deg"), "camera.fov_y_deg", positive=True)
    if fov >= 179:
        raise AppearanceInputError("camera.fov_y_deg must be below 179 degrees")

    source = job.get("area_source")
    if not isinstance(source, Mapping) or source.get("shape") != "rectangle":
        raise AppearanceInputError("a rectangular area source with a full pose is required")
    position, normal, up = (_vec(source.get(key), f"area_source.{key}")
                            for key in ("position_mm", "normal", "up"))
    sizes = np.asarray(source.get("size_mm"), dtype=np.float64)
    normal_norm = np.linalg.norm(normal)
    if (sizes.shape != (2,) or not np.isfinite(sizes).all() or np.any(sizes <= 0) or
            normal_norm < 1e-12 or np.linalg.norm(np.cross(normal, up)) < 1e-9):
        raise AppearanceInputError("area source size, normal, and up must define a nondegenerate rectangle")
    source_target = np.array([(bounds[0] + bounds[1]) * 0.5,
                              (bounds[2] + bounds[3]) * 0.5, solid_mm * 0.5])
    if np.dot(normal / normal_norm, source_target - position) <= 0:
        raise AppearanceInputError("area source normal must face the substrate")
    env, env_meta = _spectrum(job.get("environment_spectrum"), "environment_spectrum", waves)
    src, src_meta = _spectrum(source.get("spectrum"), "area_source.spectrum", waves)
    source_activity = {"area": bool(np.any(src > 0)),
                       "environment": bool(np.any(env > 0))}
    active_kinds = {meta["kind"] for active, meta in (
        (source_activity["area"], src_meta),
        (source_activity["environment"], env_meta)) if active}
    if len(active_kinds) > 1:
        raise AppearanceInputError(
            "active area and environment spectra use different units; convert them to one shared radiometric scale")
    spectral_units_kind = next(iter(active_kinds), "relative")
    if spectral_units_kind == "absolute":
        spectral_units_calibration = "absolute_radiometric"
    else:
        spectral_units_calibration = "uncalibrated relative scale"

    normalized_front = _face_layers(job.get("front_layers"), "front", waves, bounds,
                                    circle_radius_mm=circle_radius)
    normalized_back = _face_layers(raw_back_layers, "back", waves, bounds,
                                   circle_radius_mm=circle_radius)
    circle_grids = {}
    if circle_radius is not None:
        from .circle import BOUNDARY_POLICY, prepare_circle_cells
        coated = normalized_front + normalized_back
        if coated and substrate.get("circle_boundary_sampling") != BOUNDARY_POLICY:
            raise AppearanceInputError("coated circle requires explicit circle_boundary_sampling=" + BOUNDARY_POLICY)
        if coated:
            reference = coated[0]
            if any(not np.array_equal(reference[key], row[key])
                   for row in coated[1:] for key in ("x_mm", "y_mm", "valid_mask", "valid_cell_mask")):
                raise AppearanceInputError("all circle layers on both faces must share captured grid and validity masks")
        circle_grids = {"front": prepare_circle_cells(normalized_front, circle_radius),
                        "back": prepare_circle_cells(normalized_back, circle_radius)}
    back["status"] = "coated" if normalized_back else "uncoated"

    return {
        "wavelength_nm": waves,
        "substrate": {"bounds_mm": bounds, "solid_thickness_mm": solid_mm,
                      "solid_thickness_provenance": thickness_provenance,
                      "material": substrate_material, "back_surface": back,
                      "geometry_note": geometry_note,
                      **({"outline_kind": "circle", "radius_mm": circle_radius,
                          "circle_boundary_sampling": substrate.get("circle_boundary_sampling"),
                          "circle_grids": circle_grids} if circle_radius is not None else {})},
        "front_layers": normalized_front, "back_layers": normalized_back,
        "camera": {"position_mm": cam_pos, "target_mm": cam_target, "up": cam_up,
                   "fov_y_deg": fov},
        "area_source": {"position_mm": position, "normal": normal / normal_norm, "up": up,
                        "size_mm": sizes, "spectrum_values": src, **src_meta},
        "environment_values": env, "environment_metadata": env_meta,
        "source_activity": source_activity,
        "spectral_units_kind": spectral_units_kind,
        "spectral_units_calibration": spectral_units_calibration,
        "resolution": {"width": width, "height": height, "spp": spp, "seed": seed},
        "input_warnings": [
            *( ["explicit lossless assumption used for one or more materials"] if
               substrate_material["assumed_lossless"] or any(
                   layer["material"]["assumed_lossless"] for layer in normalized_front + normalized_back) else []),
            *( [geometry_note] if geometry_note else []),
            *(["circle boundary thickness uses an explicit captured-node preview approximation; it is not measured edge thickness"]
              if circle_radius is not None and normalized_front + normalized_back else []),
        ],
        "provenance": dict(job.get("provenance", {})) if isinstance(job.get("provenance", {}), Mapping) else {},
    }
