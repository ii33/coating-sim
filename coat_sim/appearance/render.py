"""Float32, scalar_rgb per-wavelength Mitsuba renderer and closed flat slab."""

from __future__ import annotations

import json
import hashlib
import math
import struct
import sys
from functools import lru_cache
from collections import OrderedDict
import zlib
from pathlib import Path
from typing import Any

import numpy as np

from .contract import AppearanceInputError, validate_job


_BSDF_REGISTERED = False


def coherent_stack_rta(wavelength_nm: float, n_substrate: complex,
                       layers_substrate_outward, angle_deg: float, *,
                       incident_from_outside: bool):
    """Evaluate a FilmDesign-order stack with actual forward/reverse interface order."""
    from ..core.film_optics import coherent_rta
    layers = list(layers_substrate_outward)
    if incident_from_outside:
        layers.reverse()
    indices = [complex(1.0), *[complex(n) for n, _ in layers], complex(n_substrate)]
    thickness = [float(d) for _, d in layers]
    if not incident_from_outside:
        indices = [complex(n_substrate), *[complex(n) for n, _ in layers], complex(1.0)]
    return coherent_rta(wavelength_nm, indices, thickness, angle_deg=angle_deg,
                        polarization="unpolarized")


@lru_cache(maxsize=256)
def _interface_angle_luts(wavelength_nm: float, n_substrate: float, packed_layers: tuple):
    """Precompute exact project TMM powers, then interpolate angles at ray hits."""
    angles = np.linspace(0.0, 89.999, 181, dtype=np.float64)
    stack = [(complex(n, k), float(d)) for n, k, d in packed_layers]
    values = np.empty((2, angles.size, 3), dtype=np.float64)
    for side, from_outside in enumerate((True, False)):
        for index, angle in enumerate(angles):
            rta = coherent_stack_rta(wavelength_nm, n_substrate, stack, float(angle),
                                     incident_from_outside=from_outside)
            values[side, index] = (rta["R"], rta["T"], rta["A"])
    return angles, values


def interpolated_angle_rta(wavelength_nm: float, n_substrate: float,
                          layers_substrate_outward, angle_deg: float, *,
                          incident_from_outside: bool):
    """Return R/T/A from the cached table without interpolating across TIR.

    Diagnostic approximation only; production ray hits do not use this LUT.
    A half-degree grid does not bound errors for thick multilayers. Within one lookup
    interval of a critical angle, and throughout the TIR domain, evaluate the
    project TMM directly so an interpolated nonzero transmission cannot cross
    the physical propagation boundary.
    """
    layers = tuple((complex(n), float(d)) for n, d in layers_substrate_outward)
    incident_n, transmitted_n = ((1.0, float(n_substrate)) if incident_from_outside
                                 else (float(n_substrate), 1.0))
    eta = incident_n / transmitted_n
    angle = float(np.clip(angle_deg, 0.0, 89.999))
    if eta > 1.0:
        critical = math.degrees(math.asin(1.0 / eta))
        # The table spacing is 0.5 degrees. Do not blend a sample below the
        # critical angle with the zero-transmission branch above it.
        if angle >= critical - 0.5:
            exact = coherent_stack_rta(wavelength_nm, n_substrate, layers, angle,
                                       incident_from_outside=incident_from_outside)
            sin2_t = eta * eta * max(0.0, 1.0 - math.cos(math.radians(angle)) ** 2)
            if sin2_t >= 1.0 - 8.0 * np.finfo(np.float64).eps:
                exact["T"] = 0.0
                exact["A"] = max(0.0, 1.0 - exact["R"])
            return exact
    packed = tuple((n.real, n.imag, d) for n, d in layers)
    angles, values = _interface_angle_luts(float(wavelength_nm), float(n_substrate), packed)
    side = 0 if incident_from_outside else 1
    result = [float(np.interp(angle, angles, values[side, :, channel])) for channel in range(3)]
    return {"R": result[0], "T": result[1], "A": result[2]}


def radiance_eta_squared(n_incident: float, n_transmitted: float) -> float:
    """Adjoint radiance correction for a smooth transmission event."""
    if not (math.isfinite(n_incident) and math.isfinite(n_transmitted) and
            n_incident > 0 and n_transmitted > 0):
        raise ValueError("indices must be finite and positive")
    return (n_incident / n_transmitted) ** 2


def _refract_local_direction(wi, cos_theta_t: float, eta_i_over_t: float):
    """Return a smooth transmitted direction for either local surface side.

    Mitsuba's ``refract`` helper emits into local -Z. Here the closed slab has
    front and back faces with opposite geometric normals, so the outgoing Z
    sign must oppose ``wi.z``. ``wi`` points away from the interface toward
    the previous path vertex; its tangential components therefore change sign.
    """
    wi_z = float(wi.z)
    return (-eta_i_over_t * float(wi.x),
            -eta_i_over_t * float(wi.y),
            -math.copysign(float(cos_theta_t), wi_z))


def _snell_transmitted_cosine(cos_theta_i: float, n_incident: float,
                              n_transmitted: float) -> tuple[float, float] | None:
    """Return ``(eta_i_over_t, cos_t)`` or ``None`` at/above criticality."""
    ci = min(1.0, max(0.0, abs(float(cos_theta_i))))
    eta = float(n_incident) / float(n_transmitted)
    sin2_t = eta * eta * max(0.0, 1.0 - ci * ci)
    if sin2_t >= 1.0 - 8.0 * np.finfo(np.float64).eps:
        return None
    return eta, math.sqrt(max(0.0, 1.0 - sin2_t))


def _transmitted_direction(wi, n_incident: float, n_transmitted: float):
    """Return a unit local transmitted direction, or ``None`` for TIR."""
    snell = _snell_transmitted_cosine(float(wi.z), n_incident, n_transmitted)
    if snell is None:
        return None
    eta, cos_t = snell
    return _refract_local_direction(wi, cos_t, eta)


def _select_rta_branch(sample, R, T, allow_reflection, allow_transmission):
    """Return (is_reflection, sampling_pdf, physical_weight) for a BSDF context."""
    if allow_reflection and allow_transmission:
        if sample < R:
            return True, R, 1.0
        if sample < R + T:
            return False, T, 1.0
        return None
    if allow_reflection and R > 0:
        return True, 1.0, R
    if allow_transmission and T > 0:
        return False, 1.0, T
    return None


def _circle_stack_at_hit(grid, indices, cache, hit_x_mm, hit_y_mm):
    """Select one immutable stack; keep at most 128 prepared cell tuples."""
    from .circle import circle_cell_index
    iy, ix = circle_cell_index(grid["x_mm"], grid["y_mm"], hit_x_mm, hit_y_mm)
    if not grid["active_cell_mask"][iy, ix]:
        raise AppearanceInputError("circle BSDF hit selected a cell with no physical disk area")
    key = (iy, ix)
    cached = cache.get(key)
    if cached is not None:
        cache.move_to_end(key)
        return cached
    thickness = grid["thickness_cell_nm"][:, iy, ix]
    if not np.isfinite(thickness).all():
        raise AppearanceInputError("circle BSDF hit selected missing actual thickness")
    stack = tuple((n, float(d)) for n, d in zip(indices, thickness))
    cache[key] = stack
    if len(cache) > 128:
        cache.popitem(last=False)
    return stack


def _register_bsdf(mi):
    global _BSDF_REGISTERED
    if _BSDF_REGISTERED:
        return

    class CoherentStackBSDF(mi.BSDF):
        """Delta interface whose branch powers come from the project coherent TMM."""

        def __init__(self, props):
            mi.BSDF.__init__(self, props)
            self.wavelength_nm = float(props.get("wavelength_nm"))
            self.ambient_n = float(props.get("ambient_n", 1.0))
            self.substrate_n = float(props.get("substrate_n"))
            self.layer_count = int(props.get("layer_count", 0))
            self.indices = [complex(float(props.get(f"layer_n_{i}")),
                                    float(props.get(f"layer_k_{i}")))
                            for i in range(self.layer_count)]
            self.thickness_nm = [float(props.get(f"layer_d_{i}"))
                                 for i in range(self.layer_count)]
            circle_json = props.get("circle_grid_json", "")
            self.circle_grid = None
            self.circle_stack_cache = OrderedDict()
            if circle_json:
                payload = json.loads(circle_json)
                self.circle_grid = {"x_mm": np.asarray(payload["x_mm"], dtype=np.float64),
                                    "y_mm": np.asarray(payload["y_mm"], dtype=np.float64),
                                    "active_cell_mask": np.asarray(payload["active_cell_mask"], dtype=bool),
                                    "thickness_cell_nm": np.asarray(payload["thickness_cell_nm"], dtype=np.float64)}
                for array in self.circle_grid.values():
                    array.setflags(write=False)
            self.m_flags = (mi.BSDFFlags.DeltaReflection | mi.BSDFFlags.DeltaTransmission |
                            mi.BSDFFlags.FrontSide | mi.BSDFFlags.BackSide)
            self.m_components = [mi.BSDFFlags.DeltaReflection, mi.BSDFFlags.DeltaTransmission]
            if not math.isfinite(self.wavelength_nm) or self.wavelength_nm <= 0:
                raise ValueError("TMM BSDF wavelength must be positive")
            if self.ambient_n <= 0 or self.substrate_n <= 0:
                raise ValueError("TMM BSDF ports must be transparent positive-index media")

        def _rta(self, si):
            wi_z = float(si.wi.z)
            from_inside = wi_z < 0.0
            n_i = self.substrate_n if from_inside else self.ambient_n
            n_t = self.ambient_n if from_inside else self.substrate_n
            ci = min(1.0, max(0.0, abs(wi_z)))
            angle = math.degrees(math.acos(ci))
            angle = min(89.999, angle)
            stack = (_circle_stack_at_hit(self.circle_grid, self.indices, self.circle_stack_cache,
                                           float(si.p.x) * 1e3, float(si.p.y) * 1e3)
                     if self.circle_grid is not None else list(zip(self.indices, self.thickness_nm)))
            rta = coherent_stack_rta(
                self.wavelength_nm, self.substrate_n,
                stack, angle,
                incident_from_outside=not from_inside)
            R, T, A = (float(rta[key]) for key in ("R", "T", "A"))
            if min(R, T, A) < -1e-9 or max(R, T, A) > 1.0 + 1e-8:
                raise ValueError("coherent TMM returned non-passive interface power")
            if not math.isclose(R + T + A, 1.0, rel_tol=0.0, abs_tol=2e-7):
                raise ValueError("coherent TMM power balance failed")
            return max(0.0, R), max(0.0, T), n_i, n_t

        def sample(self, ctx, si, sample1, sample2, active=True):
            bs = mi.BSDFSample3f()
            black = mi.Color3f(0.0)
            if not active:
                return bs, black
            R, T, n_i, n_t = self._rta(si)
            allow_r = bool(ctx.is_enabled(mi.BSDFFlags.Reflection, 0))
            allow_t = bool(ctx.is_enabled(mi.BSDFFlags.Transmission, 1))
            # When both lobes are enabled, q_R=R and q_T=T; the remaining mass
            # is absorption. A restricted context uses q=1 and retains its
            # physical branch weight, as required for component sampling.
            branch = _select_rta_branch(float(sample1), R, T, allow_r, allow_t)
            if branch is None:
                return bs, black
            reflection, probability, branch_weight = branch
            bs.sampled_component = 0 if reflection else 1
            if reflection:
                bs.wo = mi.reflect(si.wi)
                bs.sampled_type = mi.UInt32(int(mi.BSDFFlags.DeltaReflection))
                bs.eta = 1.0
            else:
                direction = _transmitted_direction(si.wi, n_i, n_t)
                if T <= 0.0 or direction is None:
                    return bs, black
                bs.wo = mi.Vector3f(*direction)
                bs.sampled_type = mi.UInt32(int(mi.BSDFFlags.DeltaTransmission))
                # Mitsuba stores transmitted / incident index for path RR.
                # Radiance throughput below uses the inverse squared.
                bs.eta = n_t / n_i
                if ctx.mode == mi.TransportMode.Radiance:
                    branch_weight *= radiance_eta_squared(n_i, n_t)
            bs.pdf = probability
            return bs, mi.Color3f(branch_weight)

        def eval(self, ctx, si, wo, active=True):
            return mi.Color3f(0.0)

        def pdf(self, ctx, si, wo, active=True):
            return 0.0

    mi.register_bsdf("appearance_coherent_stack", lambda props: CoherentStackBSDF(props))
    _BSDF_REGISTERED = True


def _look_at(mi, position, target, up):
    return mi.ScalarTransform4f.look_at(origin=position, target=target, up=up)


def _oriented_rect(mi, center, normal, up, size_x, size_y):
    # Mitsuba ScalarTransform4f.look_at aligns local +Z with target-origin.
    transform = _look_at(mi, center, np.asarray(center) + np.asarray(normal), up)
    return transform @ mi.ScalarTransform4f.scale([size_x / 2.0, size_y / 2.0, 1.0])


def _plugin_bsdf(wavelength_nm, substrate_material, layers, wave_index):
    result = {"type": "appearance_coherent_stack", "wavelength_nm": float(wavelength_nm),
              "ambient_n": 1.0, "substrate_n": float(substrate_material["n_values"][wave_index]),
              "layer_count": len(layers)}
    for index, (material, thickness_nm) in enumerate(layers):
        result[f"layer_n_{index}"] = float(material["n_values"][wave_index])
        result[f"layer_k_{index}"] = float(material["k_values"][wave_index])
        result[f"layer_d_{index}"] = float(thickness_nm)
    return result


def _cell_layer_data(layers, iy, ix):
    output = []
    for layer in layers:
        values = layer["thickness_nm"]
        # Each Mitsuba shape is a cell; use its four captured node values as a
        # piecewise-constant cell average. The project field is never replaced
        # with the nominal FilmDesign thickness.
        d = float(np.mean(values[iy:iy + 2, ix:ix + 2], dtype=np.float64))
        output.append((layer["material"], d))
    return output


def _add_surface_cells(scene, mi, prefix, z_mm, normal, layers,
                       bounds_mm, substrate_material, wave_nm, wave_index):
    xmin, xmax, ymin, ymax = bounds_mm
    if layers:
        x, y = layers[0]["x_mm"], layers[0]["y_mm"]
        for layer in layers[1:]:
            if not (np.array_equal(x, layer["x_mm"]) and np.array_equal(y, layer["y_mm"])):
                raise AppearanceInputError("all layers on one face must share the captured process grid")
    else:
        x, y = np.asarray([xmin, xmax]), np.asarray([ymin, ymax])
    rotation = (mi.ScalarTransform4f.rotate([0, 1, 0], 180)
                if normal[2] < 0 else mi.ScalarTransform4f())
    for iy in range(y.size - 1):
        for ix in range(x.size - 1):
            if layers:
                stack = _cell_layer_data(layers, iy, ix)
            else:
                stack = []
            cx, cy = (float(x[ix] + x[ix + 1]) / 2.0,
                      float(y[iy] + y[iy + 1]) / 2.0)
            dx, dy = float(x[ix + 1] - x[ix]), float(y[iy + 1] - y[iy])
            center = [cx * 1e-3, cy * 1e-3, z_mm * 1e-3]
            transform = (mi.ScalarTransform4f.translate(center) @ rotation @
                         mi.ScalarTransform4f.scale([dx * 5e-4, dy * 5e-4, 1.0]))
            key = f"{prefix}_{iy}_{ix}"
            scene[key] = {"type": "rectangle", "to_world": transform,
                          "bsdf": _plugin_bsdf(wave_nm, substrate_material, stack, wave_index)}


def _plugin_circle_bsdf(wavelength_nm, substrate_material, layers, grid, wave_index):
    # All materials and actual cell arrays are frozen once per wavelength;
    # ray hits only select an existing cell and a bounded cached stack tuple.
    result = _plugin_bsdf(wavelength_nm, substrate_material,
                          [(layer["material"], 0.0) for layer in layers], wave_index)
    values = grid["thickness_cell_nm"].astype(object)
    values[:, ~grid["active_cell_mask"]] = None
    result["circle_grid_json"] = json.dumps({
        "x_mm": grid["x_mm"].tolist(), "y_mm": grid["y_mm"].tolist(),
        "active_cell_mask": grid["active_cell_mask"].tolist(),
        "thickness_cell_nm": values.tolist()}, separators=(",", ":"), allow_nan=False)
    return result


def _add_circle_body(scene, mi, normalized, wavelength_nm, wave_index):
    substrate = normalized["substrate"]
    radius_m = substrate["radius_mm"] * 1e-3
    thickness_m = substrate["solid_thickness_mm"] * 1e-3
    material = substrate["material"]
    for face, z_m, flip, layers in (("front", 0.0, True, normalized["front_layers"]),
                                    ("back", thickness_m, False, normalized["back_layers"])):
        bsdf = (_plugin_circle_bsdf(wavelength_nm, material, layers,
                                   substrate["circle_grids"][face], wave_index)
                if layers else _plugin_bsdf(wavelength_nm, material, [], wave_index))
        transform = (mi.ScalarTransform4f.translate([0.0, 0.0, z_m]) @
                     mi.ScalarTransform4f.scale([radius_m, radius_m, radius_m]))
        scene[face + "_disk"] = {"type": "disk", "flip_normals": flip,
                                  "to_world": transform, "bsdf": bsdf}
    # Native Mitsuba cylinder is an open lateral surface. The two disks above
    # supply its only caps; side normals are outward and no coat is inferred.
    scene["side_cylinder"] = {"type": "cylinder", "p0": [0.0, 0.0, 0.0],
                              "p1": [0.0, 0.0, thickness_m], "radius": radius_m,
                              "flip_normals": False,
                              "bsdf": _plugin_bsdf(wavelength_nm, material, [], wave_index)}


def _scene_dict(mi, normalized, wavelength_nm, env_value, source_value):
    substrate = normalized["substrate"]
    xmin, xmax, ymin, ymax = substrate["bounds_mm"]
    solid_mm = substrate["solid_thickness_mm"]
    substrate_material = substrate["material"]
    scene: dict[str, Any] = {"type": "scene"}
    # Closed boundary: front/back have their own stack; all four vertical
    # sides are bare substrate/air interfaces and remain valid for TIR paths.
    wave_index = int(np.argmin(np.abs(normalized["wavelength_nm"] - wavelength_nm)))
    if substrate.get("outline_kind") == "circle":
        _add_circle_body(scene, mi, normalized, wavelength_nm, wave_index)
    else:
        _add_surface_cells(scene, mi, "front", 0.0, [0, 0, -1], normalized["front_layers"],
                           substrate["bounds_mm"], substrate_material, wavelength_nm, wave_index)
        back_layers = normalized["back_layers"] if substrate["back_surface"]["status"] == "coated" else []
        if back_layers:
            _add_surface_cells(scene, mi, "back", solid_mm, [0, 0, 1], back_layers,
                               substrate["bounds_mm"], substrate_material, wavelength_nm, wave_index)
        else:
            transform = mi.ScalarTransform4f.translate([(xmax+xmin)*5e-4, (ymax+ymin)*5e-4, solid_mm * 1e-3]) @ mi.ScalarTransform4f.scale(
                [(xmax - xmin) * 5e-4, (ymax - ymin) * 5e-4, 1.0])
            scene["back_bare"] = {"type": "rectangle", "to_world": transform,
                                  "bsdf": _plugin_bsdf(wavelength_nm, substrate_material, [], wave_index)}
        side_specs = [
            ("side_xp", [xmax * 1e-3, (ymax + ymin) * 5e-4, solid_mm * 5e-4], [1, 0, 0], [0, 0, 1], ymax-ymin, solid_mm),
            ("side_xm", [xmin * 1e-3, (ymax + ymin) * 5e-4, solid_mm * 5e-4], [-1, 0, 0], [0, 0, 1], ymax-ymin, solid_mm),
            ("side_yp", [(xmax + xmin) * 5e-4, ymax * 1e-3, solid_mm * 5e-4], [0, 1, 0], [0, 0, 1], xmax-xmin, solid_mm),
            ("side_ym", [(xmax + xmin) * 5e-4, ymin * 1e-3, solid_mm * 5e-4], [0, -1, 0], [0, 0, 1], xmax-xmin, solid_mm),
        ]
        for name, center, normal, up, sx, sy in side_specs:
            transform = _oriented_rect(mi, center, normal, up, sx * 1e-3, sy * 1e-3)
            scene[name] = {"type": "rectangle", "to_world": transform,
                           "bsdf": _plugin_bsdf(wavelength_nm, substrate_material, [], wave_index)}

    camera = normalized["camera"]
    width, height = normalized["resolution"]["width"], normalized["resolution"]["height"]
    scene["sensor"] = {
        "type": "perspective", "fov": camera["fov_y_deg"], "fov_axis": "y",
        # Scene lengths are metres. Mitsuba's default 0.01 m can place a
        # close-up camera's ray origin on or behind the front interface.
        # Explicit 1 micrometre clipping is shared by CPU and GPU scenes.
        "near_clip": 1e-6,
        "to_world": _look_at(mi, camera["position_mm"] * 1e-3,
                             camera["target_mm"] * 1e-3, camera["up"]),
        "sampler": {"type": "independent", "sample_count": normalized["resolution"]["spp"]},
        "film": {"type": "hdrfilm", "width": width, "height": height,
                 "pixel_format": "rgb", "component_format": "float32"},
    }

    source = normalized["area_source"]
    position = source["position_mm"] * 1e-3
    normal, up = source["normal"], source["up"]
    target_direction = np.array([(xmin + xmax) * 5e-4, (ymin + ymax) * 5e-4, solid_mm * 5e-4]) - position
    if np.dot(normal, target_direction) <= 0:
        raise AppearanceInputError("area source normal must face the substrate")
    source_transform = _oriented_rect(mi, position, normal, up,
                                      source["size_mm"][0] * 1e-3,
                                      source["size_mm"][1] * 1e-3)
    scene["area"] = {"type": "rectangle", "to_world": source_transform,
                     "emitter": {"type": "area", "radiance":
                                 {"type": "uniform", "value": float(source_value)}}}
    scene["environment"] = {"type": "constant", "radiance":
                            {"type": "uniform", "value": float(env_value)}}
    scene["integrator"] = {"type": "path", "max_depth": 12, "rr_depth": 5}
    return scene


def _cmf_table(wavelengths_nm: np.ndarray) -> np.ndarray:
    try:
        import mitsuba as mi
    except ImportError:
        # Mitsuba/DrJit stay installed in their dedicated environment. The
        # project interpreter supplies NumPy; this process-local search path
        # loads only the isolated renderer packages and changes no PATH state.
        site_packages = Path(__file__).resolve().parents[2] / "build" / "mitsuba_appearance_env" / "Lib" / "site-packages"
        if site_packages.is_dir():
            sys.path.insert(0, str(site_packages))
            import mitsuba as mi
        else:
            raise RuntimeError("Mitsuba is unavailable; expected build/mitsuba_appearance_env")
    mi.set_variant("scalar_spectral")
    try:
        cmf = np.asarray([mi.cie1931_xyz(float(w)) for w in wavelengths_nm], dtype=np.float64)
    finally:
        mi.set_variant("scalar_rgb")
    if cmf.shape != (wavelengths_nm.size, 3) or not np.isfinite(cmf).all():
        raise RuntimeError("Mitsuba scalar_spectral returned invalid CIE 1931 CMFs")
    return cmf


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)


def write_png(path: str | Path, rgb8: np.ndarray) -> None:
    array = np.asarray(rgb8, dtype=np.uint8)
    if array.ndim != 3 or array.shape[2] != 3:
        raise ValueError("PNG pixels must be HxWx3 uint8")
    h, w, _ = array.shape
    raw = b"".join(b"\x00" + array[row].tobytes() for row in range(h))
    payload = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + _png_chunk(b"IDAT", zlib.compress(raw, 9)) + _png_chunk(b"IEND", b"")
    Path(path).write_bytes(payload)


def _canonical_thickness(row, *, circle=False):
    if not circle:
        return row["thickness_nm"].tolist()
    values = row["thickness_nm"].astype(object)
    values[~row["valid_mask"]] = None
    return values.tolist()


def _load_mitsuba():
    try:
        import mitsuba as mi
    except ImportError:
        site_packages = Path(__file__).resolve().parents[2] / "build" / "mitsuba_appearance_env" / "Lib" / "site-packages"
        if not site_packages.is_dir():
            raise RuntimeError("Mitsuba is unavailable; expected build/mitsuba_appearance_env")
        sys.path.insert(0, str(site_packages))
        import mitsuba as mi
    # Mitsuba's sample block layout depends on thread count. Keep serial and
    # spawned workers identical, not merely identical RNG seed integers.
    import drjit as dr
    dr.set_thread_count(1)
    mi.set_variant("scalar_rgb")
    _register_bsdf(mi)
    return mi


def _render_one_wave(mi, data, index):
    wavelength = float(data["wavelength_nm"][index])
    scene_dict = _scene_dict(mi, data, wavelength,
                             data["environment_values"][index],
                             data["area_source"]["spectrum_values"][index])
    scene = mi.load_dict(scene_dict)
    seed = int(data["resolution"]["seed"] + index)
    image = np.asarray(mi.render(scene, spp=data["resolution"]["spp"], seed=seed), dtype=np.float32)
    height, width = data["resolution"]["height"], data["resolution"]["width"]
    if image.shape != (height, width, 3) or not np.isfinite(image).all():
        raise RuntimeError(f"Mitsuba returned an invalid linear film at {wavelength:g} nm")
    error = max(float(np.max(np.abs(image[..., 0] - image[..., 1]))),
                float(np.max(np.abs(image[..., 1] - image[..., 2]))))
    return index, image[..., 0].copy(), error, seed


def _serial_wave_results(mi, data, cancel_event):
    for index in range(len(data["wavelength_nm"])):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("appearance render cancelled at wavelength boundary")
        yield _render_one_wave(mi, data, index)


def render_job(job: dict, output_dir: str | Path, *, normalized: bool = False,
               cancel_event=None, display_exposure: float = 0.01, workers: int = 1,
               progress_callback=None) -> dict:
    """Render one physically sampled monochromatic stack per wavelength.

    Result arrays are float32 linear spectral radiance, XYZ, and linear sRGB.
    The Mitsuba ``scalar_rgb`` film is a three-channel carrier; each wavelength
    is injected as an equal-valued uniform RGB texture and one channel is read.
    """
    from .parallel import validate_workers, iter_parallel_waves
    workers = validate_workers(workers)
    data = job if normalized else validate_job(job)
    from .progress import RenderProgress
    progress = RenderProgress(len(data["wavelength_nm"]), progress_callback)

    def check_cancel(stage):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("appearance render cancelled " + stage)

    check_cancel("before runtime startup")
    output = Path(output_dir)
    if output.exists():
        raise FileExistsError(f"render output directory already exists; use a fresh job directory: {output}")
    display_exposure = float(display_exposure)
    if not math.isfinite(display_exposure) or display_exposure <= 0:
        raise ValueError("display_exposure must be finite and positive")
    mi = _load_mitsuba()
    wavelengths = data["wavelength_nm"]
    height, width = data["resolution"]["height"], data["resolution"]["width"]
    output.mkdir(parents=True, exist_ok=False)
    spectrum = np.lib.format.open_memmap(output / "radiance_spectrum.npy", mode="w+",
                                         dtype=np.float32, shape=(wavelengths.size, height, width))
    try:
        progress.emit()
        channel_equality_max = 0.0
        seeds = [None] * wavelengths.size
        if workers == 1:
            results = _serial_wave_results(mi, data, cancel_event)
        else:
            results = iter_parallel_waves(data, workers, cancel_event)
        try:
            for index, plane, error, seed in results:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("appearance render cancelled at wavelength boundary")
                channel_equality_max = max(channel_equality_max, error)
                spectrum[index] = plane
                seeds[index] = seed
                progress.advance()
        finally:
            results.close()
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("appearance render cancelled before result integration")
        spectrum.flush()
        progress.emit("integrating")
        check_cancel("before color integration")
        cmf = _cmf_table(wavelengths)
        check_cancel("after color matching functions")
        xyz = np.trapezoid(spectrum[..., None] * cmf[:, None, None, :], x=wavelengths, axis=0).astype(np.float32)
        # IEC 61966-2-1 linear sRGB matrix, D65 / CIE 1931 2 degree observer.
        xyz_to_rgb = np.array([[3.2406, -1.5372, -0.4986],
                               [-0.9689, 1.8758, 0.0415],
                               [0.0557, -0.2040, 1.0570]], dtype=np.float32)
        linear_rgb = np.einsum("...j,ij->...i", xyz, xyz_to_rgb, optimize=True).astype(np.float32)
        check_cancel("before saving color arrays")
        np.save(output / "xyz.npy", xyz)
        np.save(output / "linear_srgb.npy", linear_rgb)
        write_png(output / "display.png", display_rgb(linear_rgb, display_exposure))
        circle = data["substrate"].get("outline_kind") == "circle"
        canonical_payload = {
            "wavelength_nm": wavelengths.tolist(),
            "resolution": data["resolution"],
            "substrate": {"bounds_mm": data["substrate"]["bounds_mm"].tolist(),
                          "solid_thickness_mm": data["substrate"]["solid_thickness_mm"],
                          "solid_thickness_provenance": data["substrate"]["solid_thickness_provenance"],
                          "back_surface": {"status": data["substrate"]["back_surface"]["status"],
                                           "outside_medium": data["substrate"]["back_surface"].get("outside_medium", "air")},
                          "material": {key: np.asarray(value).tolist() if isinstance(value, np.ndarray) else value
                                       for key, value in data["substrate"]["material"].items()}},
            "front_layers": [{"layer_id": row["layer_id"],
                              "x_mm": row["x_mm"].tolist(), "y_mm": row["y_mm"].tolist(),
                              "thickness_nm": _canonical_thickness(row, circle=circle),
                              "valid_mask": row["valid_mask"].tolist(),
                              **({"valid_cell_mask": row["valid_cell_mask"].tolist()} if circle else {}),
                              "material_n": row["material"]["n_values"].tolist(),
                              "material_k": row["material"]["k_values"].tolist()} for row in data["front_layers"]],
            "back_layers": [{"layer_id": row["layer_id"],
                             "x_mm": row["x_mm"].tolist(), "y_mm": row["y_mm"].tolist(),
                             "thickness_nm": _canonical_thickness(row, circle=circle),
                             "valid_mask": row["valid_mask"].tolist(),
                             **({"valid_cell_mask": row["valid_cell_mask"].tolist()} if circle else {}),
                             "material_n": row["material"]["n_values"].tolist(),
                             "material_k": row["material"]["k_values"].tolist()} for row in data["back_layers"]],
            "camera": {"position_mm": data["camera"]["position_mm"].tolist(),
                       "target_mm": data["camera"]["target_mm"].tolist(),
                       "up": data["camera"]["up"].tolist(), "fov_y_deg": data["camera"]["fov_y_deg"]},
            "area_source": {"position_mm": data["area_source"]["position_mm"].tolist(),
                            "normal": data["area_source"]["normal"].tolist(),
                            "up": data["area_source"]["up"].tolist(),
                            "size_mm": data["area_source"]["size_mm"].tolist(),
                            "spectrum_values": data["area_source"]["spectrum_values"].tolist(),
                            "kind": data["area_source"]["kind"], "scale": data["area_source"]["scale"],
                            "calibration": data["area_source"]["calibration"]},
            "environment_spd": data["environment_values"].tolist(),
            "environment_metadata": data["environment_metadata"],
            "provenance": data["provenance"],
        }
        if circle:
            canonical_payload["substrate"].update({
                "outline_kind": "circle", "radius_mm": data["substrate"]["radius_mm"],
                "circle_boundary_sampling": data["substrate"]["circle_boundary_sampling"]})
        input_hash = hashlib.sha256(json.dumps(canonical_payload, sort_keys=True,
                                               separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        manifest = {
            "schema_version": 1, "engine": "Mitsuba 3.9.0 scalar_rgb, independent scalar render per wavelength",
            "film_semantics": "linear_float32; RGB channels are equal scalar carrier channels; only channel 0 is spectral radiance",
            "transport": "path integrator, smooth isotropic coherent stack delta BSDF, unpolarized intensity R/T/A; radiance transmission applies eta_i_over_t squared",
            "execution": {"backend": "cpu_scalar", "workers": min(workers, int(wavelengths.size)),
                          "threads_per_process": 1,
                          "wavelength_order": "input_index", "seed_rule": "base_seed + wavelength_index"},
            "angular_optics": {"method": "exact_per_hit_coherent_tmm",
                               "angular_interpolation": False, "grazing_cap_deg": 89.999},
            "coordinates": {"frame": "process-local", "input_unit": "mm", "engine_unit": "m", "front_outward_normal": "-Z", "solid_body": "+Z"},
            "spectral_units": ("W m^-2 sr^-1 nm^-1" if data["spectral_units_kind"] == "absolute" else "relative radiance per wavelength"),
            "source_kind": data["spectral_units_kind"],
            "source_calibration": data["spectral_units_calibration"],
            "source_activity": data["source_activity"],
            "area_source_metadata": {"kind": data["area_source"]["kind"],
                                     "calibration": data["area_source"]["calibration"]},
            "environment_source_metadata": data["environment_metadata"],
            "input_sha256": input_hash,
            "wavelength_nm": wavelengths.tolist(), "resolution": data["resolution"],
            "per_wavelength_seed": seeds, "film_rgb_equality_max_abs": channel_equality_max,
            "substrate_geometry": data["substrate"]["geometry_note"] or "explicit solid body geometry input",
            "back_surface": data["substrate"]["back_surface"]["status"],
            "back_uncoated_model": "substrate/air coherent Fresnel interface" if data["substrate"]["back_surface"]["status"] == "uncoated" else None,
            "input_warnings": data["input_warnings"],
            "input_provenance": data["provenance"],
            "outputs": {"spectrum": "radiance_spectrum.npy [lambda,y,x], float32, linear",
                        "xyz": "xyz.npy [y,x,XYZ], float32, linear, CIE 1931 2 degree, trapezoidal wavelength integration",
                        "linear_srgb": "linear_srgb.npy [y,x,RGB], float32, linear",
                        "display": "display.png, view-only tone map",
                        "display_exposure": display_exposure},
        }
        if circle:
            radius, solid = data["substrate"]["radius_mm"], data["substrate"]["solid_thickness_mm"]
            manifest["circle_geometry"] = {
                "model": "native_analytic_disk_cylinder", "radius_mm": radius,
                "solid_thickness_mm": solid, "disk_area_mm2": math.pi * radius ** 2,
                "solid_volume_mm3": math.pi * radius ** 2 * solid,
                "polygon_tessellation": None, "polygon_sagitta_mm": 0.0,
                "front_outward_normal": "-Z", "back_outward_normal": "+Z",
                "side": "open_native_cylinder; bare_substrate_air; outward_radial_normal",
                "caps": "front_disk_and_back_disk_only"}
            manifest["circle_sampling"] = {
                face: data["substrate"]["circle_grids"][face]["metadata"] for face in ("front", "back")}
        manifest["timing"] = progress.emit("complete")
        check_cancel("before completion publication")
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"radiance_spectrum": spectrum, "xyz": xyz, "linear_srgb": linear_rgb,
                "manifest": manifest, "output_dir": output}
    except BaseException:
        spectrum._mmap.close()
        raise


def display_rgb(linear_rgb: np.ndarray, exposure: float = 1.0) -> np.ndarray:
    """Apply a fixed exposure, simple Reinhard view transform, and sRGB encoding."""
    values = np.maximum(0.0, np.asarray(linear_rgb, dtype=np.float64) * float(exposure))
    mapped = values / (1.0 + values)
    encoded = np.where(mapped <= 0.0031308, 12.92 * mapped,
                       1.055 * np.power(mapped, 1.0 / 2.4) - 0.055)
    return np.clip(np.rint(encoded * 255.0), 0, 255).astype(np.uint8)
