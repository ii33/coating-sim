"""Fast geometry previews for the fixed-frame correction-plate designer.

The preview uses the same source law and planetary kinematics as ``simulate``.
It deliberately samples a few substrate rings rather than the full field, so
its blocking weights are estimates for interactive design work.
"""

import math
from dataclasses import replace

import numpy as np

from .machine_profiles import validate_mask_profile
from .simulator import _blocked, _planet_surface_pose, minimum_substrate_z
from .source_profiles import (source_samples, effective_source_exponent,
                              source_cover, source_visibility)
from .substrate_profiles import substrate_mask, validate_substrate_profile
from .substrate_surface import surface_height_and_normal, validate_substrate_surface


def _active_surface(settings, supplied=None):
    if settings.substrate_profile is not None:
        return validate_substrate_profile(settings.substrate_profile)["surface"]
    return validate_substrate_surface(
        settings.substrate_surface if supplied is None else supplied,
        settings.substrate_radius_mm)


def _minimum_substrate_z(settings, surface):
    return minimum_substrate_z(settings, surface)


def _mask_height(settings, mask_z_mm):
    settings.validate()
    z = float(mask_z_mm)
    minimum_z = _minimum_substrate_z(settings, _active_surface(settings))
    if not math.isfinite(z) or not 0 < z < minimum_z:
        raise ValueError(f"板高必须在 0 和基片最低点 {minimum_z:g} mm 之间")
    return z


def _preview_time_count(requested):
    """Choose a prime sample count per carrier revolution for a small patch."""
    count = min(max(requested, 599), 719)
    while count > 2:
        if all(count % divisor for divisor in range(2, math.isqrt(count) + 1)):
            return count
        count -= 1
    return 2


def _return_cycle_phases(settings, samples_per_revolution):
    """Sample a full material-point return cycle in carrier phase.

    The carrier returns after ``Np / gcd(Ns, Np)`` revolutions: its own
    angle has made an integer number of turns, and the extra planet spin
    ``Ns / Np`` has also made an integer number. Each carrier revolution has
    the same number of stratified samples. Deterministic offsets within the
    strata reduce phase-locking to narrow mask features.
    """
    revolutions = settings.planet_teeth // math.gcd(
        settings.sun_teeth, settings.planet_teeth)
    phase_count = revolutions * samples_per_revolution
    indices = np.arange(phase_count, dtype=float)
    golden_fraction = (math.sqrt(5) - 1) / 2
    phase_revolutions = (
        indices + np.mod(indices * golden_fraction + 0.5, 1.0)
    ) / samples_per_revolution
    carrier_angle = (math.radians(settings.carrier_phase_deg) +
                     2 * math.pi * phase_revolutions)
    planet_angle = (math.radians(settings.planet_phase_deg) +
                    2 * math.pi * (1 + settings.sun_teeth / settings.planet_teeth)
                    * phase_revolutions)
    return revolutions, phase_count, carrier_angle, planet_angle


def _ring_surface(settings, surface, radius, local_azimuths,
                  planet_angle, carrier_angle):
    """Planet-relative XYZ and source-facing normal for a material ring.

    Positive tilt raises the side toward the carrier center.  The spin is
    applied first, then the entire curved substrate is rotated about the
    tangential carrier axis.  The same rigid rotation is applied to normals.
    """
    local_x = radius * np.cos(local_azimuths)
    local_y = radius * np.sin(local_azimuths)
    height, nx, ny, nz = surface_height_and_normal(
        local_x, local_y, settings.substrate_radius_mm, surface)
    return _planet_surface_pose(
        np, local_x[None, :], local_y[None, :],
        np.asarray(height)[None, :], np.asarray(nx)[None, :],
        np.asarray(ny)[None, :], np.asarray(nz)[None, :],
        planet_angle[:, None], carrier_angle[:, None], settings)


def _source_flux(settings, dx, dy, delta_z, normal_x, normal_y, normal_z):
    distance_squared = dx * dx + dy * dy + delta_z * delta_z
    distance = np.sqrt(distance_squared)
    source_cosine = np.maximum(0.0, delta_z / distance)
    receiver_cosine = np.maximum(
        0.0, -(normal_x * dx + normal_y * dy + normal_z * delta_z) / distance)
    return source_cosine ** settings.source_exponent * receiver_cosine / distance_squared


def center_cut_trajectory(settings, mask_z_mm):
    """Trace the planet-center obstruction ring across the mask plane.

    Coordinates are fixed machine XY in mm. For a complete carrier revolution
    the locus is a circle, so ``points_xy`` depicts it once; ``start_xy`` and
    ``end_xy`` still refer to the actual deposition interval.
    """
    z = _mask_height(settings, mask_z_mm)
    source_z = float(getattr(settings, "source_z_mm", 0.0))
    if z <= source_z:
        raise ValueError("源位于修正板高度或其上方，没有经过该板的上行切割轨迹")
    fraction = (z - source_z) / (settings.substrate_z_mm - source_z)
    center_x = settings.source_x_mm + fraction * (settings.carrier_x_mm - settings.source_x_mm)
    center_y = settings.source_y_mm + fraction * (settings.carrier_y_mm - settings.source_y_mm)
    radius = fraction * settings.planet_offset_mm
    phase = math.radians(settings.carrier_phase_deg)
    sweep = 2 * math.pi * settings.carrier_rpm * settings.duration_s / 60
    full_revolution = abs(sweep) >= 2 * math.pi

    start = (center_x + radius * math.cos(phase),
             center_y + radius * math.sin(phase))
    end_angle = phase + math.remainder(sweep, 2 * math.pi)
    end = (center_x + radius * math.cos(end_angle),
           center_y + radius * math.sin(end_angle))

    if radius == 0 or sweep == 0:
        points = np.asarray([start], dtype=float)
    else:
        depicted_sweep = math.copysign(2 * math.pi, sweep) if full_revolution else sweep
        # About one sample per degree, with a cap for responsive redraws.
        count = min(361, max(2, math.ceil(abs(math.degrees(depicted_sweep))) + 1))
        angles = phase + np.linspace(0, depicted_sweep, count)
        points = np.column_stack((center_x + radius * np.cos(angles),
                                  center_y + radius * np.sin(angles)))

    return {
        "points_xy": points,
        "center_xy": (center_x, center_y),
        "radius_mm": radius,
        "start_xy": start,
        "end_xy": end,
        "full_revolution": full_revolution,
        "sweep_degrees": math.degrees(sweep),
    }


def radial_blocking_weights(settings, mask_z_mm, point_xy, patch_radius_mm,
                            radii_mm=None, source_profile=None,
                            substrate_surface=None):
    """Estimate a selected point's potential obstruction for each disk radius.

    The selected point represents the center of a *circular patch* in the mask
    plane. A ring's weight is the unmasked source flux carried by rays crossing
    that patch, divided by all unmasked source flux to the ring. Every element
    in a finite source footprint contributes according to its quadrature
    weight. Existing mask
    polygons are deliberately excluded. Each ring averages fixed material
    points around the disk over one complete material-point return cycle.
    """
    z = _mask_height(settings, mask_z_mm)
    surface = _active_surface(settings, substrate_surface)
    substrate = (validate_substrate_profile(settings.substrate_profile)
                 if settings.substrate_profile is not None else None)
    point = np.asarray(point_xy, dtype=float)
    if point.shape != (2,) or not np.isfinite(point).all():
        raise ValueError("分析点必须是有限的 [x, y] 坐标")
    patch_radius = float(patch_radius_mm)
    max_patch_radius = 2 * max(10000.0, settings.substrate_radius_mm,
                               settings.planet_offset_mm)
    if not math.isfinite(patch_radius) or not 0 < patch_radius <= max_patch_radius:
        raise ValueError(f"局部区域半径必须在 0 和 {max_patch_radius:g} mm 之间")

    if radii_mm is None:
        radii = np.linspace(0, settings.substrate_radius_mm, 11)
    else:
        radii = np.asarray(radii_mm, dtype=float)
        if (radii.ndim != 1 or radii.size == 0 or
                not np.isfinite(radii).all() or
                np.any(radii < 0) or np.any(radii > settings.substrate_radius_mm)):
            raise ValueError("采样半径必须在行星盘半径范围内")

    # A narrow selected patch needs denser sampling than a complete board.
    samples_per_revolution = _preview_time_count(settings.time_steps)
    azimuth_count = 48
    revolutions, phase_count, carrier_angle, planet_angle = _return_cycle_phases(
        settings, samples_per_revolution)
    planet_x = settings.carrier_x_mm + settings.planet_offset_mm * np.cos(carrier_angle)
    planet_y = settings.carrier_y_mm + settings.planet_offset_mm * np.sin(carrier_angle)
    local_azimuths = 2 * math.pi * np.arange(azimuth_count, dtype=float) / azimuth_count
    source_positions, source_weights = source_samples(settings, source_profile)
    cover = source_cover(source_profile, settings)
    if cover and settings.source_z_mm + cover["cover_height_mm"] + cover["cover_thickness_mm"] >= _minimum_substrate_z(settings, surface):
        raise ValueError("蒸发舟盖板顶部须低于工件盘最低点")
    settings = replace(settings, source_exponent=effective_source_exponent(settings, source_profile))
    weights = np.empty(radii.size, dtype=float)

    for i, radius in enumerate(radii):
        valid_ring = (substrate_mask(radius * np.cos(local_azimuths),
                                     radius * np.sin(local_azimuths), substrate)
                      if substrate is not None else np.ones(azimuth_count, dtype=bool))
        if not np.any(valid_ring):
            weights[i] = np.nan
            continue
        total = 0.0
        blocked = 0.0
        for start in range(0, phase_count, 128):
            stop = min(start + 128, phase_count)
            offset_x, offset_y, target_z, nx, ny, nz = _ring_surface(
                settings, surface, radius, local_azimuths,
                planet_angle[start:stop], carrier_angle[start:stop])
            x = planet_x[start:stop, None] + offset_x
            y = planet_y[start:stop, None] + offset_y
            if np.any(target_z <= z):
                raise ValueError("修正板高度不得达到当前曲面基片高度")
            for (source_x, source_y, source_z), source_weight in zip(source_positions, source_weights):
                dx = x - source_x
                dy = y - source_y
                delta_z = target_z - source_z
                if np.any(delta_z <= 0):
                    continue
                flux = np.where(valid_ring[None, :],
                                _source_flux(settings, dx, dy, delta_z, nx, ny, nz), 0.0)
                if cover is not None:
                    flux = np.where(source_visibility(np, x, y, target_z, float(source_x),
                                    float(source_y), float(source_z), cover), flux, 0.0)
                total += float(source_weight * np.sum(flux))
                if source_z < z:
                    fraction = (z - source_z) / delta_z
                    mask_x = source_x + fraction * dx
                    mask_y = source_y + fraction * dy
                    hit = np.hypot(mask_x - point[0], mask_y - point[1]) <= patch_radius
                    blocked += float(source_weight * np.sum(flux * hit))
        if not math.isfinite(total) or total <= 0:
            raise ValueError("预览通量不可计算，请检查几何参数")
        weights[i] = 100 * blocked / total

    return {
        "radii_mm": radii,
        "weights_percent": weights,
        "time_steps_used": phase_count,
        "time_samples": phase_count,
        "return_carrier_revolutions": revolutions,
        "phase_sample_count": phase_count,
        "samples_per_revolution": samples_per_revolution,
        "azimuth_samples": azimuth_count,
        "source_samples": len(source_weights),
        "point_count": int(radii.size * azimuth_count),
        "source_sample_count": len(source_weights),
    }


def correction_board_blocking_weights(settings, mask, radii_mm=None,
                                      source_profile=None,
                                      substrate_surface=None):
    """Estimate the flux blocked by this complete board at substrate radii.

    The denominator is the unmasked flux from one selected source. The
    numerator uses the same zero-thickness polygon, hole and boundary rules as
    the simulator. Other boards and other sources are deliberately excluded.
    This sampled preview covers one complete material-point return cycle.
    """
    settings.validate()
    surface = _active_surface(settings, substrate_surface)
    substrate = (validate_substrate_profile(settings.substrate_profile)
                 if settings.substrate_profile is not None else None)
    minimum_z = _minimum_substrate_z(settings, surface)
    board = validate_mask_profile(mask, substrate_z_mm=minimum_z)
    if radii_mm is None:
        radii = np.linspace(0, settings.substrate_radius_mm, 11)
    else:
        radii = np.asarray(radii_mm, dtype=float)
        if (radii.ndim != 1 or radii.size == 0 or
                not np.isfinite(radii).all() or
                np.any(radii < 0) or np.any(radii > settings.substrate_radius_mm)):
            raise ValueError("采样半径必须在基片半径范围内")

    # The complete board can use fewer phase samples per revolution than a
    # small selected patch while still covering the full return cycle.
    samples_per_revolution = 79
    azimuth_count = 24
    revolutions, phase_count, carrier_angle, planet_angle = _return_cycle_phases(
        settings, samples_per_revolution)
    planet_x = settings.carrier_x_mm + settings.planet_offset_mm * np.cos(carrier_angle)
    planet_y = settings.carrier_y_mm + settings.planet_offset_mm * np.sin(carrier_angle)
    local_azimuths = 2 * math.pi * np.arange(azimuth_count) / azimuth_count
    source_positions, source_weights = source_samples(settings, source_profile)
    cover = source_cover(source_profile, settings)
    if cover and settings.source_z_mm + cover["cover_height_mm"] + cover["cover_thickness_mm"] >= _minimum_substrate_z(settings, surface):
        raise ValueError("蒸发舟盖板顶部须低于工件盘最低点")
    settings = replace(settings, source_exponent=effective_source_exponent(settings, source_profile))
    weights = np.empty(len(radii), dtype=float)

    for index, radius in enumerate(radii):
        valid_ring = (substrate_mask(radius * np.cos(local_azimuths),
                                     radius * np.sin(local_azimuths), substrate)
                      if substrate is not None else np.ones(azimuth_count, dtype=bool))
        if not np.any(valid_ring):
            weights[index] = np.nan
            continue
        unmasked_flux = 0.0
        blocked_flux = 0.0
        for start in range(0, phase_count, 128):
            stop = min(start + 128, phase_count)
            offset_x, offset_y, target_z, nx, ny, nz = _ring_surface(
                settings, surface, radius, local_azimuths,
                planet_angle[start:stop], carrier_angle[start:stop])
            x = planet_x[start:stop, None] + offset_x
            y = planet_y[start:stop, None] + offset_y
            if np.any(target_z <= board["z_mm"]):
                raise ValueError("修正板高度不得达到当前曲面基片高度")
            for (source_x, source_y, source_z), source_weight in zip(source_positions, source_weights):
                dx = x - source_x
                dy = y - source_y
                delta_z = target_z - source_z
                if np.any(delta_z <= 0):
                    continue
                flux = np.where(valid_ring[None, :],
                                _source_flux(settings, dx, dy, delta_z, nx, ny, nz), 0.0)
                if cover is not None:
                    flux = np.where(source_visibility(np, x, y, target_z, float(source_x),
                                    float(source_y), float(source_z), cover), flux, 0.0)
                unmasked_flux += float(source_weight * np.sum(flux))
                if source_z < board["z_mm"]:
                    blocked = _blocked(np, x, y, board, float(source_x),
                                       float(source_y), target_z, float(source_z))
                    blocked_flux += float(source_weight * np.sum(flux * blocked))
        if not math.isfinite(unmasked_flux) or unmasked_flux <= 0:
            raise ValueError("预览通量不可计算，请检查几何参数")
        weights[index] = 100 * blocked_flux / unmasked_flux

    # The drawn planet-center ring is geometric.  Whether its material point
    # actually has the largest blockage must be decided from the flux-weighted
    # result, not inferred from the ring alone.  This comparison is meaningful
    # here only for the flat workpiece used by the mask preview.  The maximum
    # is over the sampled radii, so a narrow off-center peak can be missed.
    valid = np.flatnonzero(np.isfinite(weights))
    center = np.flatnonzero(np.isclose(radii, 0.0, rtol=0.0, atol=1e-9) &
                            np.isfinite(weights))
    peak_index = int(valid[np.argmax(weights[valid])]) if valid.size else None
    center_index = int(center[0]) if center.size else None
    flat_surface = surface["kind"] == "flat"
    center_is_peak = None
    if (flat_surface and center_index is not None and peak_index is not None
            and weights[peak_index] > 0):
        center_is_peak = bool(np.isclose(
            weights[center_index], weights[peak_index],
            rtol=1e-6, atol=1e-8))

    return {"radii_mm": radii, "weights_percent": weights,
            "flat_surface": flat_surface,
            "center_weight_percent": (float(weights[center_index])
                                      if center_index is not None else None),
            "radial_peak_weight_percent": (float(weights[peak_index])
                                            if peak_index is not None else None),
            "radial_peak_radius_mm": (float(radii[peak_index])
                                       if peak_index is not None else None),
            "center_is_radial_peak": center_is_peak,
            "time_samples": phase_count,
            "return_carrier_revolutions": revolutions,
            "phase_sample_count": phase_count,
            "samples_per_revolution": samples_per_revolution,
            "azimuth_samples": azimuth_count,
            "source_samples": len(source_weights),
            "point_count": int(radii.size * azimuth_count),
            "source_sample_count": len(source_weights)}


def correction_board_blocking_field(settings, mask, source_profile=None,
                                    substrate_surface=None, *,
                                    ring_radii_mm=None, angular_samples=73,
                                    grid_size=41):
    """Full-return-cycle board blockage at fixed material points on a disk.

    Returns angular curves at selected radii and a Cartesian disk map.  Each
    point's denominator is its own unmasked flux, not a ring-wide average.
    The preview intentionally excludes other boards and other sources.
    """
    settings.validate()
    surface = _active_surface(settings, substrate_surface)
    substrate = (validate_substrate_profile(settings.substrate_profile)
                 if settings.substrate_profile is not None else None)
    board = validate_mask_profile(
        mask, substrate_z_mm=_minimum_substrate_z(settings, surface))
    radius = float(settings.substrate_radius_mm)
    if ring_radii_mm is None:
        ring_radii = radius * np.asarray([0.25, 0.5, 0.75, 1.0])
    else:
        ring_radii = np.asarray(ring_radii_mm, dtype=float)
        if (ring_radii.ndim != 1 or not 1 <= ring_radii.size <= 8 or
                not np.isfinite(ring_radii).all() or
                np.any(ring_radii < 0) or np.any(ring_radii > radius)):
            raise ValueError("角向分析半径须为工件盘范围内的 1–8 个值")
    if not 9 <= angular_samples <= 361 or not 9 <= grid_size <= 201:
        raise ValueError("角向或网格采样数超出预览范围")

    # Same stratified phase rule and per-revolution resolution as the radial
    # board view. Geometry is evaluated in phase/point blocks below.
    samples_per_revolution = 79
    revolutions, phase_count, carrier_angle, planet_angle = _return_cycle_phases(
        settings, samples_per_revolution)
    planet_x = settings.carrier_x_mm + settings.planet_offset_mm * np.cos(carrier_angle)
    planet_y = settings.carrier_y_mm + settings.planet_offset_mm * np.sin(carrier_angle)
    source_positions, source_weights = source_samples(settings, source_profile)
    cover = source_cover(source_profile, settings)
    if cover and settings.source_z_mm + cover["cover_height_mm"] + cover["cover_thickness_mm"] >= _minimum_substrate_z(settings, surface):
        raise ValueError("蒸发舟盖板顶部须低于工件盘最低点")
    settings = replace(settings, source_exponent=effective_source_exponent(settings, source_profile))

    def evaluate(local_x, local_y):
        local_x = np.asarray(local_x, dtype=float).reshape(-1)
        local_y = np.asarray(local_y, dtype=float).reshape(-1)
        valid = np.hypot(local_x, local_y) <= radius + 1e-9
        if substrate is not None:
            valid &= substrate_mask(local_x, local_y, substrate)
        result = np.full(local_x.shape, np.nan, dtype=float)
        if not np.any(valid):
            return result
        valid_indices = np.flatnonzero(valid)
        for point_start in range(0, valid_indices.size, 128):
            point_indices = valid_indices[point_start:point_start + 128]
            lx, ly = local_x[point_indices], local_y[point_indices]
            height, nx, ny, nz = surface_height_and_normal(
                lx, ly, radius, surface)
            height = np.asarray(height, dtype=float)
            nx, ny, nz = (np.asarray(v, dtype=float) for v in (nx, ny, nz))
            total = np.zeros(lx.shape, dtype=float)
            blocked_total = np.zeros(lx.shape, dtype=float)
            for phase_start in range(0, phase_count, 64):
                phase_stop = min(phase_start + 64, phase_count)
                px, py, target_z, world_nx, world_ny, world_nz = _planet_surface_pose(
                    np, lx[None, :], ly[None, :], height[None, :],
                    nx[None, :], ny[None, :], nz[None, :],
                    planet_angle[phase_start:phase_stop, None],
                    carrier_angle[phase_start:phase_stop, None], settings)
                x = planet_x[phase_start:phase_stop, None] + px
                y = planet_y[phase_start:phase_stop, None] + py
                if np.any(target_z <= board["z_mm"]):
                    raise ValueError("修正板高度不得达到当前曲面基片高度")
                for (source_x, source_y, source_z), source_weight in zip(source_positions, source_weights):
                    dx, dy = x - source_x, y - source_y
                    delta_z = target_z - source_z
                    if np.any(delta_z <= 0):
                        continue
                    flux = _source_flux(settings, dx, dy, delta_z,
                                        world_nx, world_ny, world_nz)
                    if cover is not None:
                        flux = np.where(source_visibility(np, x, y, target_z, float(source_x),
                                        float(source_y), float(source_z), cover), flux, 0.0)
                    total += source_weight * np.sum(flux, axis=0)
                    if source_z < board["z_mm"]:
                        hit = _blocked(np, x, y, board, float(source_x),
                                       float(source_y), target_z, float(source_z))
                        blocked_total += source_weight * np.sum(flux * hit, axis=0)
            positive = np.isfinite(total) & (total > 0)
            values = np.full(lx.shape, np.nan, dtype=float)
            values[positive] = 100 * blocked_total[positive] / total[positive]
            result[point_indices] = values
        return result

    azimuths_deg = np.linspace(0.0, 360.0, angular_samples)
    angles = np.deg2rad(azimuths_deg)
    polar_x = (ring_radii[:, None] * np.cos(angles)[None, :]).reshape(-1)
    polar_y = (ring_radii[:, None] * np.sin(angles)[None, :]).reshape(-1)
    angular_weights = evaluate(polar_x, polar_y).reshape(
        ring_radii.size, angular_samples)
    grid_axis = np.linspace(-radius, radius, grid_size)
    grid_x, grid_y = np.meshgrid(grid_axis, grid_axis)
    grid_weights = evaluate(grid_x, grid_y).reshape(grid_size, grid_size)
    valid_weights = grid_weights[np.isfinite(grid_weights)]
    maximum_weight = float(np.max(valid_weights)) if valid_weights.size else 0.0
    relative_weights = np.full_like(grid_weights, np.nan)
    if maximum_weight > 0:
        relative_weights[np.isfinite(grid_weights)] = (
            100.0 * grid_weights[np.isfinite(grid_weights)] / maximum_weight)
    else:
        relative_weights[np.isfinite(grid_weights)] = 0.0
    return {
        "ring_radii_mm": ring_radii,
        "azimuths_deg": azimuths_deg,
        "angular_weights_percent": angular_weights,
        "grid_x_mm": grid_axis,
        "grid_y_mm": grid_axis,
        "grid_weights_percent": grid_weights,
        "relative_grid_weights_percent": relative_weights,
        "relative_normalization_max_percent": maximum_weight,
        "workpiece_diameter_mm": 2 * radius,
        "time_samples": phase_count,
        "return_carrier_revolutions": revolutions,
        "phase_sample_count": phase_count,
        "samples_per_revolution": samples_per_revolution,
        "point_count": int(polar_x.size + grid_x.size),
        "source_sample_count": len(source_weights),
        "source_samples": len(source_weights),
    }
