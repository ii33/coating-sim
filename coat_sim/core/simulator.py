"""Exploratory planetary evaporation model; geometry is in millimetres.

The point-source angular law is illustrative, not a calibrated deposition model.
The returned field is normalized to a mean of one over the sampled substrate.
"""

from dataclasses import dataclass
import json
import math
from pathlib import Path

import numpy as np

from .gear_geometry import center_distance_from_module, module_from_center_distance
from .source_profiles import (source_samples, effective_source_exponent,
                              source_cover, source_visibility)
from .substrate_profiles import (effective_radius_mm, substrate_mask,
                                validate_substrate_profile)
from .substrate_surface import surface_height_and_normal, validate_substrate_surface


class _NoFluxError(ValueError):
    """A valid calculation produced no unoccluded substrate samples."""


@dataclass(frozen=True)
class Settings:
    carrier_rpm: float = 15.0
    sun_teeth: int = 120
    planet_teeth: int = 36
    duration_s: float = 1.0
    time_steps: int = 600
    grid_size: int = 181
    substrate_radius_mm: float = 150.0
    planet_offset_mm: float = 312.0
    substrate_z_mm: float = 1000.0
    carrier_x_mm: float = 0.0
    carrier_y_mm: float = 0.0
    source_x_mm: float = 100.0
    source_y_mm: float = 0.0
    source_z_mm: float = 0.0
    source_exponent: float = 1.0
    carrier_phase_deg: float = 0.0
    planet_phase_deg: float = 0.0
    planet_tilt_deg: float = 0.0
    gear_module_mm: float | None = None
    substrate_surface: dict | None = None
    substrate_profile: dict | None = None
    source_rate_nm_s: float | None = None
    beam_attenuation_m_inv: float | None = None

    @property
    def effective_gear_module_mm(self) -> float:
        """Actual module implied by the center distance and tooth counts.

        The optional stored module allows older radius-only Settings to remain
        valid.  Their effective module is calculated from the radius.
        """
        return module_from_center_distance(
            self.planet_offset_mm, self.sun_teeth, self.planet_teeth)

    def validate(self):
        if self.beam_attenuation_m_inv is not None and (isinstance(self.beam_attenuation_m_inv, bool) or not math.isfinite(self.beam_attenuation_m_inv) or self.beam_attenuation_m_inv < 0):
            raise ValueError("直达束衰减系数必须为有限非负数")
        if not (-300 <= self.carrier_rpm <= 300):
            raise ValueError("大盘转速必须在 -300–300 rpm 之间")
        if not (1 <= self.sun_teeth <= 1000 and 1 <= self.planet_teeth <= 1000):
            raise ValueError("齿数必须在 1–1000 之间")
        if not (0 < self.duration_s <= 86400):
            raise ValueError("沉积时长必须在 0–86400 秒之间")
        if type(self.time_steps) is not int or self.time_steps < 1:
            raise ValueError("时间步数必须为正整数")
        if type(self.grid_size) is not int or self.grid_size < 21:
            raise ValueError("网格边长必须为不小于 21 的整数")
        if not (0 < self.substrate_radius_mm <= 10000 and
                0 < self.substrate_z_mm <= 100000 and
                0 <= self.planet_offset_mm <= 10000):
            raise ValueError("半径、盘心距离或基片高度超出有效范围")
        if self.source_rate_nm_s is not None:
            try:
                valid_rate = (not isinstance(self.source_rate_nm_s, bool) and
                              math.isfinite(self.source_rate_nm_s) and self.source_rate_nm_s > 0)
            except (TypeError, ValueError, OverflowError):
                valid_rate = False
            if not valid_rate:
                raise ValueError("源参考沉积速率必须为有限正数")
        if not (0 <= self.source_exponent <= 20):
            raise ValueError("源角指数必须在 0–20 之间")
        if not -80 <= self.planet_tilt_deg <= 80:
            raise ValueError("行星盘径向倾角须在 -80–80° 之间")
        if self.substrate_profile is not None:
            profile = validate_substrate_profile(self.substrate_profile)
            radius = effective_radius_mm(profile)
            if not math.isclose(self.substrate_radius_mm, radius,
                                rel_tol=1e-9, abs_tol=1e-6):
                raise ValueError("计算半径须与基片轮廓最大半径一致")
            surface = profile["surface"]
        else:
            surface = validate_substrate_surface(self.substrate_surface,
                                                 self.substrate_radius_mm)
        radial = np.linspace(0.0, self.substrate_radius_mm, 257)
        if minimum_substrate_z(self, surface) <= self.source_z_mm:
            raise ValueError("实体基片在整个倾斜盘范围内必须高于蒸发源")
        if self.gear_module_mm is not None:
            expected_offset = center_distance_from_module(
                self.gear_module_mm, self.sun_teeth, self.planet_teeth)
            if not math.isclose(self.planet_offset_mm, expected_offset,
                                rel_tol=1e-9, abs_tol=1e-6):
                raise ValueError("齿轮模数与行星盘轨道半径不一致")
        for name, value in vars(self).items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{name} 必须为有限数值")


def minimum_substrate_z(settings: Settings, surface=None) -> float:
    """Lowest possible world Z over a radial surface and the tilted disk.

    The full circular radial envelope is conservative for noncircular apertures.
    Positive tilt raises the side toward the carrier centre.
    """
    if surface is None:
        surface = (validate_substrate_profile(settings.substrate_profile)["surface"]
                   if settings.substrate_profile is not None else
                   validate_substrate_surface(settings.substrate_surface,
                                              settings.substrate_radius_mm))
    radial = np.linspace(0.0, settings.substrate_radius_mm, 257)
    heights = surface_height_and_normal(radial, 0.0,
                                         settings.substrate_radius_mm, surface)[0]
    angle = math.radians(settings.planet_tilt_deg)
    return settings.substrate_z_mm + float(np.min(
        heights * math.cos(angle) - radial * abs(math.sin(angle))))


def _planet_surface_pose(xp, local_x, local_y, local_height, local_nx,
                         local_ny, local_nz, plate_angle, carrier_angle,
                         settings):
    """World displacement and source-facing normal for a radially tilted plate.

    ``plate_angle`` and ``carrier_angle`` have shape [time, 1], local arrays
    [1, point]. The +tilt side nearest the carrier centre rises in world Z.
    """
    cp, sp = xp.cos(plate_angle), xp.sin(plate_angle)
    px = local_x * cp - local_y * sp
    py = local_x * sp + local_y * cp
    nx = local_nx * cp - local_ny * sp
    ny = local_nx * sp + local_ny * cp
    inward_x, inward_y = -xp.cos(carrier_angle), -xp.sin(carrier_angle)
    u = px * inward_x + py * inward_y
    nu = nx * inward_x + ny * inward_y
    angle = math.radians(settings.planet_tilt_deg)
    c, s = math.cos(angle), math.sin(angle)
    offset = u * (c - 1.0) - local_height * s
    normal_offset = nu * (c - 1.0) - local_nz * s
    return (px + inward_x * offset, py + inward_y * offset,
            settings.substrate_z_mm + u * s + local_height * c,
            nx + inward_x * normal_offset,
            ny + inward_y * normal_offset, nu * s + local_nz * c)


def read_mask(path: str):
    """Read a fixed-frame, zero-thickness polygon with optional holes.

    {"z_mm": 500, "outer": [[x,y], ...], "holes": [[[x,y], ...], ...]}
    The mask blocks a ray if its intersection is inside outer and outside holes.
    """
    if not path.strip():
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    z = float(data["z_mm"])
    if not math.isfinite(z):
        raise ValueError("修正板高度必须为有限数值")

    def polygon(points):
        p = np.asarray(points, dtype=float)
        if p.ndim != 2 or p.shape[1] != 2 or not 3 <= p.shape[0] <= 500 or not np.isfinite(p).all():
            raise ValueError("多边形需要 3–500 个有限坐标 [x,y]")
        # Reject degenerate polygons; the even-odd rule supports concave shapes.
        area = np.sum(p[:, 0] * np.roll(p[:, 1], -1) - np.roll(p[:, 0], -1) * p[:, 1])
        if abs(area) < 1e-9:
            raise ValueError("多边形面积为零")
        return p

    outer = polygon(data["outer"])
    holes = [polygon(h) for h in data.get("holes", [])]
    if len(holes) > 30:
        raise ValueError("最多支持 30 个孔洞")
    return {"z_mm": z, "outer": outer, "holes": holes}


def _mask_layers(mask):
    """Return every selected fixed-frame correction-board polygon."""
    if mask is None:
        return ()
    if isinstance(mask, dict):
        return (mask,)
    if isinstance(mask, (list, tuple)) and all(
            isinstance(layer, dict) for layer in mask):
        return tuple(mask)
    raise ValueError("修正板必须是单个板配置或板配置的列表")


def workload(settings: Settings, mask=None, source_profile=None, progress=None):
    """Dimensionless operation index, not a predicted duration."""
    source_count = len(source_samples(settings, source_profile)[1])
    effective_source_exponent(settings, source_profile)
    vertices = sum(len(layer["outer"]) + sum(map(len, layer["holes"]))
                   for layer in _mask_layers(mask))
    disk_points = math.pi / 4 * settings.grid_size ** 2
    surface = (validate_substrate_profile(settings.substrate_profile)["surface"]
               if settings.substrate_profile is not None else
               validate_substrate_surface(settings.substrate_surface,
                                          settings.substrate_radius_mm))
    surface_factor = 1.0 if surface["kind"] == "flat" else 1.5
    return (settings.time_steps * disk_points * source_count *
            (1 + 0.6 * vertices) * surface_factor / 1_000_000)


def _inside_polygon(xp, x, y, poly):
    # Even-odd crossings; an exact boundary is treated as inside (blocked).
    result = xp.zeros_like(x, dtype=bool)
    boundary = xp.zeros_like(x, dtype=bool)
    for i in range(len(poly)):
        ax, ay = poly[i - 1]
        bx, by = poly[i]
        cross = (x - ax) * (by - ay) - (y - ay) * (bx - ax)
        if getattr(xp, "is_directml", False):
            # Float32 roundoff grows with the coordinate scale; use a local
            # error bound so edge tests stay stable on DirectML devices.
            eps = 4 * 1.1920928955078125e-7
            x_tol = (xp.abs(x) + max(abs(ax), abs(bx), 1.0)) * eps + 1e-8
            y_tol = (xp.abs(y) + max(abs(ay), abs(by), 1.0)) * eps + 1e-8
            cross_tol = (xp.abs((x - ax) * (by - ay)) +
                         xp.abs((y - ay) * (bx - ax))) * eps + 1e-8
        else:
            x_tol = y_tol = cross_tol = 1e-8
        on_segment = ((xp.abs(cross) <= cross_tol) &
                      (x >= min(ax, bx) - x_tol) & (x <= max(ax, bx) + x_tol) &
                      (y >= min(ay, by) - y_tol) & (y <= max(ay, by) + y_tol))
        boundary |= on_segment
        if ay != by:
            crossings = ((ay > y) != (by > y)) & (x < ax + (y - ay) * (bx - ax) / (by - ay))
            result ^= crossings
    return result | boundary


def _blocked(xp, x, y, mask, sx, sy, z, source_z=0.0):
    blocked_any = xp.zeros_like(x, dtype=bool)
    for layer in _mask_layers(mask):
        if layer["z_mm"] <= source_z:
            continue
        # Only a board strictly between this source and each moving target
        # point can block its ray.
        between = z > layer["z_mm"]
        t = (layer["z_mm"] - source_z) / (z - source_z)
        ix = sx + (x - sx) * t
        iy = sy + (y - sy) * t
        blocked = between & _inside_polygon(xp, ix, iy, layer["outer"])
        for hole in layer["holes"]:
            blocked &= ~_inside_polygon(xp, ix, iy, hole)
        blocked_any |= blocked
    return blocked_any


def _backend(choice):
    if choice == "CPU":
        return np, "CPU"
    if choice not in ("自动", "GPU"):
        raise ValueError("未知计算设备")
    try:
        import cupy as cp
        if cp.cuda.runtime.getDeviceCount() > 0:
            # Exercise allocation, float64 arithmetic, and device-to-host transfer
            # before committing a full job to this CUDA installation.
            probe = cp.asarray([1.0], dtype=cp.float64)
            if float(cp.asnumpy(cp.sum(probe * probe))) != 1.0:
                raise RuntimeError("CUDA 设备探测结果异常")
            return cp, "GPU (CuPy)"
    except Exception as exc:
        if choice == "GPU":
            raise RuntimeError(
                f"CuPy 或兼容的 CUDA 设备初始化失败（{type(exc).__name__}: {exc}），请选“自动”或 CPU") from exc
        return np, "CPU（GPU初始化失败）"
    if choice == "GPU":
        raise RuntimeError("没有可用的 CUDA 设备，请选 CPU")
    return np, "CPU（未检测到可用 GPU）"


class _DirectMLBackend:
    """Small NumPy-like adapter for the operators used by this simulation."""

    is_directml = True

    def __init__(self, torch, device):
        self.torch = torch
        self.device = device
        # DirectML devices commonly lack reliable float64 support; use float32.
        self.float64 = torch.float32

    def _dtype(self, dtype):
        if dtype is bool or dtype == self.torch.bool:
            return self.torch.bool
        return self.torch.float32

    def asarray(self, values):
        values = np.asarray(values)
        dtype = self.torch.bool if values.dtype.kind == "b" else self.torch.float32
        return self.torch.as_tensor(values, dtype=dtype).to(self.device)

    def zeros(self, shape, dtype=None):
        return self.torch.zeros(shape, dtype=self._dtype(dtype)).to(self.device)

    def zeros_like(self, values, dtype=None):
        return self.torch.zeros_like(values, dtype=self._dtype(dtype))

    def arange(self, *args, dtype=None):
        return self.torch.arange(*args, dtype=self._dtype(dtype)).to(self.device)

    def abs(self, values):
        return self.torch.abs(values)

    def cos(self, values):
        return self.torch.cos(values)

    def sin(self, values):
        return self.torch.sin(values)

    def exp(self, values):
        return self.torch.exp(values)

    def sqrt(self, values):
        return self.torch.sqrt(values)

    def where(self, condition, if_true, if_false):
        return self.torch.where(condition, if_true, if_false)

    def sum(self, values, axis=0):
        return self.torch.sum(values, dim=axis)

    def asnumpy(self, values):
        return values.detach().to("cpu").numpy()


def _directml_backend():
    try:
        import torch
        import torch_directml

        device = None
        device_name = "默认设备"
        try:
            for index in range(torch_directml.device_count()):
                try:
                    # torch-directml 在部分版本返回带结尾 NUL 的字符串，
                    # 直接显示会在设备名后残留不可见字符，这里清理掉。
                    name = str(torch_directml.device_name(index)).replace("\x00", "").strip()
                except Exception:
                    continue
                if "amd" in name.lower() or "radeon" in name.lower():
                    device = torch_directml.device(index)
                    device_name = name
                    break
        except Exception:
            # Older torch-directml builds may not expose device enumeration.
            pass
        if device is None:
            device = torch_directml.device()
        xp = _DirectMLBackend(torch, device)
        # Probe the float32 math, boolean geometry operations, selection, and
        # readback used by the simulation before selecting this backend.
        probe = xp.asarray(np.asarray([0.5], dtype=np.float32))
        inside = ((probe > 0) & (probe < 1)) ^ torch.zeros_like(probe, dtype=torch.bool)
        trig_check = torch.sin(probe) ** 2 + torch.cos(probe) ** 2
        sample = torch.where(inside, torch.sqrt(probe ** 2) * trig_check,
                            torch.zeros_like(probe))
        if not math.isclose(float(xp.asnumpy(torch.sum(sample))), 0.5, rel_tol=1e-5):
            raise RuntimeError("DirectML 设备探测结果异常")
        return xp, f"GPU (DirectML: {device_name})"
    except Exception as exc:
        raise RuntimeError(
            f"DirectML 初始化失败（{type(exc).__name__}: {exc}）；请确认 Windows 驱动和 torch-directml 环境") from exc


def _select_backend(choice):
    if choice == "DirectML":
        return _directml_backend()
    xp, device = _backend(choice)
    if choice == "自动" and xp is np:
        try:
            return _directml_backend()
        except Exception:
            # CPU remains available even if the optional DirectML package is absent.
            return np, "CPU（未检测到可用 GPU）"
    return xp, device


def _available_system_memory_bytes():
    from .compute_resources import available_memory_bytes
    return available_memory_bytes()


def _simulate_on_backend(settings, mask, xp, device, cancel_event,
                         source_positions, source_weights, progress=None,
                         cover=None, source_exponent=None):
    exponent = settings.source_exponent if source_exponent is None else source_exponent
    n = settings.grid_size
    r = settings.substrate_radius_mm
    axis = np.linspace(-r, r, n)
    local_x, local_y = np.meshgrid(axis, axis)
    if settings.substrate_profile is not None:
        profile = validate_substrate_profile(settings.substrate_profile)
        aperture = substrate_mask(local_x, local_y, profile)
        surface = profile["surface"]
    else:
        aperture = local_x ** 2 + local_y ** 2 <= r ** 2
        surface = validate_substrate_surface(settings.substrate_surface, r)
    curved_surface = surface["kind"] != "flat"
    tilted_plate = abs(settings.planet_tilt_deg) > 1e-12
    if not np.any(aperture):
        raise ValueError("基片轮廓在当前网格中没有采样点，请增大网格边长")
    lx = xp.asarray(local_x[aperture])
    ly = xp.asarray(local_y[aperture])
    local_height, local_nx, local_ny, local_nz = surface_height_and_normal(
        local_x[aperture], local_y[aperture], r, surface)
    local_height_backend = xp.asarray(local_height)[None, :]
    point_z = settings.substrate_z_mm + local_height_backend
    local_nx = xp.asarray(local_nx)[None, :]
    local_ny = xp.asarray(local_ny)[None, :]
    local_nz_backend = xp.asarray(local_nz)[None, :]
    count = len(lx)
    accumulated = xp.zeros(count, dtype=xp.float64)
    attenuation = settings.beam_attenuation_m_inv
    vacuum_accumulated = xp.zeros(count, dtype=xp.float64) if attenuation is not None else None
    time_count = settings.time_steps
    # Bound transient arrays, and use smaller batches when GPU memory is tight.
    # Curved normals and incidence add several live tensors per time point.
    live_arrays = (24 if (curved_surface or tilted_plate) else 12) + (3 if attenuation is not None else 0)
    batch_elements = 220000
    if getattr(xp, "is_directml", False):
        # An iGPU can grow into shared system RAM. The 512 MB "dedicated"
        # figure is not its allocation ceiling. Estimate temporary tensor
        # room from *available* RAM, while retaining a conservative floor and
        # ceiling because this is not the DXGI process video-memory budget.
        available = _available_system_memory_bytes()
        if available is None:
            batch_elements = 96000
        else:
            memory_budget = min(24 * 1024 ** 2,
                                max(4 * 1024 ** 2, int(available * 0.01)))
            batch_elements = min(500000, max(96000,
                                             memory_budget // (live_arrays * 4)))
    elif xp is not np:
        try:
            free_bytes, _ = xp.cuda.runtime.memGetInfo()
            # Estimate room for the live float64 arrays used in each batch.
            memory_budget = min(32 * 1024 ** 2, int(free_bytes * 0.12))
            batch_elements = min(batch_elements,
                                 max(1, memory_budget // (live_arrays * 8)))
        except Exception:
            # Keep the conservative fixed cap if memory reporting is unavailable.
            pass
    batch = max(1, min(32, batch_elements // count))
    omega_c = 2 * math.pi * settings.carrier_rpm / 60
    omega_p = omega_c * (1 + settings.sun_teeth / settings.planet_teeth)
    dt = settings.duration_s / time_count
    phi_c0 = math.radians(settings.carrier_phase_deg)
    phi_p0 = math.radians(settings.planet_phase_deg)
    for start in range(0, time_count, batch):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("计算已取消")
        end = min(start + batch, time_count)
        times = xp.arange(start, end, dtype=xp.float64)[:, None]
        carrier_angle = phi_c0 + omega_c * (times + 0.5) * dt
        plate_angle = phi_p0 + omega_p * (times + 0.5) * dt
        cx = settings.carrier_x_mm + settings.planet_offset_mm * xp.cos(carrier_angle)
        cy = settings.carrier_y_mm + settings.planet_offset_mm * xp.sin(carrier_angle)
        if tilted_plate:
            local_px, local_py, point_z, normal_x, normal_y, normal_z = \
                _planet_surface_pose(
                    xp, lx[None, :], ly[None, :], local_height_backend,
                    local_nx, local_ny, local_nz_backend,
                    plate_angle, carrier_angle, settings)
            x, y = cx + local_px, cy + local_py
        else:
            x = cx + lx[None, :] * xp.cos(plate_angle) - ly[None, :] * xp.sin(plate_angle)
            y = cy + lx[None, :] * xp.sin(plate_angle) + ly[None, :] * xp.cos(plate_angle)
        if curved_surface and not tilted_plate:
            normal_x = local_nx * xp.cos(plate_angle) - local_ny * xp.sin(plate_angle)
            normal_y = local_nx * xp.sin(plate_angle) + local_ny * xp.cos(plate_angle)
        # Keep the [time, substrate point] batch fixed while iterating source
        # elements. A [source, time, point] tensor would exhaust shared iGPU
        # memory for finite sources at practical grid sizes.
        for (source_x, source_y, source_z), source_weight in zip(source_positions, source_weights):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("计算已取消")
            dx = x - float(source_x)
            dy = y - float(source_y)
            dz = point_z - float(source_z)
            distance2 = dx ** 2 + dy ** 2 + dz ** 2
            distance = xp.sqrt(distance2)
            # Source emission is measured about +Z. Receiver incidence uses
            # the local source-facing surface normal, rotated with the planet.
            if curved_surface or tilted_plate:
                receiver_numerator = -(normal_x * dx + normal_y * dy +
                                       (normal_z if tilted_plate else local_nz_backend) * dz)
                receiver_cosine = xp.where(receiver_numerator > 0,
                                           receiver_numerator / distance, 0.0)
            else:
                receiver_cosine = dz / distance
            flux = ((dz / distance) ** exponent *
                    receiver_cosine / distance2)
            if cover is not None:
                flux = xp.where(source_visibility(xp, x, y, point_z, float(source_x),
                                float(source_y), float(source_z), cover), flux, 0.0)
            if mask is not None:
                flux = xp.where(_blocked(xp, x, y, mask, float(source_x),
                                         float(source_y), point_z, float(source_z)), 0.0, flux)
            if vacuum_accumulated is not None:
                vacuum_accumulated += xp.sum(flux, axis=0) * (dt * float(source_weight))
                if attenuation > 0:
                    flux = flux * xp.exp(-attenuation * distance * 0.001)
            accumulated += xp.sum(flux, axis=0) * (dt * float(source_weight))
        if progress is not None:
            progress(end / time_count)
    values = xp.asnumpy(accumulated) if xp is not np else accumulated
    mean = float(np.mean(values))
    if not math.isfinite(mean) or mean <= 0:
        raise _NoFluxError("所有采样点均无可表示的直达通量；请检查工作压力/截面、修正板和源位置，不能归一化完全消失的直达束" if attenuation else "所有采样点均无沉积通量；请检查修正板和源位置")
    normalized = values / mean
    field = np.full((n, n), np.nan)
    field[aperture] = normalized
    substrate_height = np.full((n, n), np.nan)
    substrate_height[aperture] = settings.substrate_z_mm + local_height
    substrate_normal_z = np.full((n, n), np.nan)
    substrate_normal_z[aperture] = local_nz
    return {
        **({"vacuum_mean_raw": float(np.mean(xp.asnumpy(vacuum_accumulated) if xp is not np else vacuum_accumulated)),
             "direct_beam_survival": mean / float(np.mean(xp.asnumpy(vacuum_accumulated) if xp is not np else vacuum_accumulated))}
            if vacuum_accumulated is not None else {}),
        "field": field, "x": axis, "y": axis, "device": device,
        "substrate_height_mm": substrate_height,
        "substrate_normal_z": substrate_normal_z,
        "time_batch_steps": batch,
        "batch_elements_limit": batch_elements,
        "mean_raw": mean, "cv_percent": float(np.std(normalized) * 100),
        "pv_percent": float((np.max(normalized) - np.min(normalized)) * 100),
        "min_percent": float(np.min(normalized) * 100),
        "max_percent": float(np.max(normalized) * 100),
        "omega_p_rpm": settings.carrier_rpm * (1 + settings.sun_teeth / settings.planet_teeth),
    }


def simulate(settings: Settings, mask=None, backend="自动", cancel_event=None,
             source_profile=None, progress=None):
    settings.validate()
    layers = _mask_layers(mask)
    surface = (validate_substrate_profile(settings.substrate_profile)["surface"]
               if settings.substrate_profile is not None else
               validate_substrate_surface(settings.substrate_surface,
                                          settings.substrate_radius_mm))
    lowest_z = minimum_substrate_z(settings, surface)
    for layer in layers:
        if layer["z_mm"] >= lowest_z:
            raise ValueError("修正板高度须低于整个倾斜工件盘的最低点")
    # A board below an individual source has no effect for that source.
    mask = layers if layers else None
    source_positions, source_weights = source_samples(settings, source_profile)
    exponent = effective_source_exponent(settings, source_profile)
    cover = source_cover(source_profile, settings)
    if cover and settings.source_z_mm + cover["cover_height_mm"] + cover["cover_thickness_mm"] >= lowest_z:
        raise ValueError("蒸发舟盖板顶部须低于整个工件盘的最低点")
    if backend not in ("自动", "GPU", "DirectML", "CPU"):
        raise ValueError("未知计算设备")
    candidates = ("GPU", "DirectML", "CPU") if backend == "自动" else (backend,)
    notices = []
    for candidate in candidates:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("计算已取消")
        try:
            xp, device = (_directml_backend() if candidate == "DirectML"
                          else _backend(candidate))
            result = _simulate_on_backend(settings, mask, xp, device, cancel_event,
                                          source_positions, source_weights, progress=progress,
                                          cover=cover, source_exponent=exponent)
        except (InterruptedError, MemoryError, _NoFluxError):
            raise
        except Exception as exc:
            if backend != "自动" or candidate == "CPU":
                raise RuntimeError(
                    f"{candidate} 计算失败：{type(exc).__name__}: {exc}") from exc
            notices.append(f"{candidate} 失败，尝试下一设备：{type(exc).__name__}: {exc}")
            continue
        if source_profile and source_profile.get("emission_model"):
            result["source_model"] = {"kind": source_profile["emission_model"]["kind"],
                                      "effective_source_exponent": exponent,
                                      "source_samples": len(source_weights),
                                      "cover_mode": "direct_view" if cover else "open",
                                      "provenance": source_profile["emission_model"].get("provenance", {}),
                                      "approximation": "静态发射足迹近似；不求解温度、壁面再发射或扫描瞬态"}
        result["backend_requested"] = backend
        result["backend_selected"] = candidate
        if notices:
            result["backend_notice"] = "；".join(notices)
            result["backend_notices"] = notices
        try:
            from .backend_memory import record_backend_success
            record_backend_success(candidate, device)
        except Exception as exc:
            # Persistence cannot turn a completed physical calculation into a failure.
            result["backend_memory_warning"] = f"成功设备记忆未保存：{type(exc).__name__}"
        return result
    raise RuntimeError("没有可用的计算设备")
