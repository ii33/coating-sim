"""Combine source-specific relative fields into a multi-step process in nm.

The existing simulator predicts spatial shape, not a calibrated deposition
rate. Each step's target divided by its user-provided rate gives its duration;
the relative field is then scaled to the target at the selected reference.
"""

from dataclasses import asdict, replace
import hashlib
import math
import json
import zipfile
from pathlib import Path

import numpy as np

from .process_environment import resolve_environment, environment_report
from .process_profiles import resolve_process_profile, validate_process_profile, step_mask_slots
from .simulator import _blocked, _mask_layers, simulate
from .source_profiles import (source_samples, effective_source_exponent,
                              source_cover, source_visibility, source_rate_cache_key)


_GEOMETRY_FIELDS = (
    "carrier_x_mm", "carrier_y_mm", "planet_offset_mm",
    "substrate_radius_mm", "substrate_z_mm", "planet_tilt_deg",
    "sun_teeth", "planet_teeth",
    "gear_module_mm", "substrate_profile",
)
FAST_CYCLE_SAMPLES_PER_REVOLUTION = 79
DEFAULT_FAST_CYCLE_RESIDUAL_PERCENT = 5.0
MAX_CAPTURED_LAYER_FIELD_BYTES = 256 * 1024 * 1024


class _LayerFieldSpool:
    """Write each completed float64 layer grid into an NPZ member immediately."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.archive = zipfile.ZipFile(self.path, mode="x",
                                       compression=zipfile.ZIP_DEFLATED,
                                       allowZip64=True)
        self.closed = False

    def write(self, array_key, values):
        with self.archive.open(array_key + ".npy", mode="w") as stream:
            np.lib.format.write_array(stream, np.asarray(values), allow_pickle=False)

    def close(self):
        if not self.closed:
            self.archive.close()
            self.closed = True

    def abort(self):
        try:
            self.close()
        finally:
            self.path.unlink(missing_ok=True)


def _layer_dependency_sha256(process, machine, settings, sampling, *, backend,
                             fast_cycle, fast_cycle_residual_percent,
                             finite_periodic, contribution_source_slot,
                             recipe_execution_algorithm,
                             recipe_execution_options):
    payload = json.dumps({
        "process": process,
        "machine": machine,
        "settings": asdict(settings),
        "sampling": sampling,
        "execution": {
            "backend": backend,
            "fast_cycle": fast_cycle,
            "fast_cycle_residual_percent": fast_cycle_residual_percent,
            "finite_periodic": finite_periodic,
            "contribution_source_slot": contribution_source_slot,
            "recipe_execution_algorithm": recipe_execution_algorithm,
            "recipe_execution_options": recipe_execution_options,
        },
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _update_layer_field_digest(digest, array_key, values):
    array = np.ascontiguousarray(values, dtype=np.float64)
    digest.update(array_key.encode("ascii"))
    digest.update(json.dumps([array.dtype.str, list(array.shape)], separators=(",", ":")).encode("ascii"))
    digest.update(memoryview(array).cast("B"))


def _stable_layer_step_id(index, step):
    """Identify a recipe step deterministically within its ordered recipe."""
    canonical = json.dumps(step, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    return f"step-{index:05d}-{hashlib.sha256(canonical).hexdigest()[:16]}"


def _film_layer_metadata(process, step):
    reference = step.get("film_layer_ref")
    if not isinstance(reference, dict):
        return {"layer_id": None, "surface": None, "material_id": None}
    layer_id, surface = reference.get("layer_id"), reference.get("surface")
    material_id = None
    snapshot = process.get("film_design_snapshot")
    design = snapshot.get("design") if isinstance(snapshot, dict) else None
    layers = design.get(f"{surface}_layers", []) if isinstance(design, dict) and surface in ("front", "back") else []
    for layer in layers:
        if isinstance(layer, dict) and layer.get("id") == layer_id:
            material_id = layer.get("material_id")
            break
    return {"layer_id": layer_id, "surface": surface, "material_id": material_id}


def _allocate_step_counts(durations, total_steps):
    """Apportion an exact global sample count across sequential steps.

    A step receives at least one midpoint sample. Remaining rounding error is
    assigned to steps with the largest quota deficit, in input order on ties.
    """
    total_duration = math.fsum(durations)
    quotas = [total_steps * (duration / total_duration) for duration in durations]
    counts = [max(1, math.floor(quota)) for quota in quotas]
    while sum(counts) < total_steps:
        index = max(range(len(counts)), key=lambda i: quotas[i] - counts[i])
        counts[index] += 1
    while sum(counts) > total_steps:
        candidates = (i for i, count in enumerate(counts) if count > 1)
        index = min(candidates, key=lambda i: quotas[i] - counts[i])
        counts[index] -= 1
    return counts


def process_timing(process, *, time_steps=None, time_step_s=None):
    """Resolve recipe duration and the two-way global time sampling controls.

    The entered step length is a maximum requested mean step length. Its
    integer count is rounded upward, then the effective mean is T/N. The
    individual step integrators use duration_i/N_i after integer allocation.
    """
    if isinstance(process, dict) and "profile" in process and "machine" in process:
        process = process["profile"]
    process = validate_process_profile(process)
    durations = [step["target_nm"] / step["rate_nm_s"]
                 for step in process["steps"]]
    total_duration = math.fsum(durations)
    if not math.isfinite(total_duration) or total_duration <= 0:
        raise ValueError("工艺总时长必须为有限正数")
    step_count = len(durations)
    if time_steps is not None and time_step_s is not None:
        raise ValueError("时间步数与时间步长一次只能指定一个")
    if time_step_s is not None:
        if isinstance(time_step_s, bool):
            raise ValueError("时间步长必须为有限正数")
        try:
            requested_step = float(time_step_s)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("时间步长必须为有限正数") from exc
        if not math.isfinite(requested_step) or requested_step <= 0:
            raise ValueError("时间步长必须为有限正数")
        ratio = total_duration / requested_step
        if not math.isfinite(ratio):
            raise ValueError("该时间步长产生非有限采样点数")
        count = max(step_count, math.ceil(ratio))
    else:
        if time_steps is None:
            time_steps = 600
        if isinstance(time_steps, bool):
            raise ValueError("时间步数必须为整数")
        try:
            numeric_count = float(time_steps)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("时间步数必须为整数") from exc
        if not math.isfinite(numeric_count) or not numeric_count.is_integer():
            raise ValueError("时间步数必须为整数")
        count = int(numeric_count)
        if count < step_count:
            raise ValueError(f"总时间步数必须不少于层数 {step_count}")
    counts = _allocate_step_counts(durations, count)
    effective_step = total_duration / count
    local_steps = [duration / step_steps
                   for duration, step_steps in zip(durations, counts)]
    if effective_step <= 0 or any(step <= 0 for step in local_steps):
        raise ValueError("推导的时间步长小到无法用浮点数表示；请降低沉积速率")
    return {
        "total_duration_s": total_duration,
        "time_steps": count,
        "time_step_s": effective_step,
        "step_durations_s": durations,
        "step_time_steps": counts,
        "step_time_step_s": local_steps,
    }


def process_sampling_plan(settings, process, *, fast_cycle=False,
                          fast_cycle_residual_percent=DEFAULT_FAST_CYCLE_RESIDUAL_PERCENT):
    """Choose real-time or shared return-cycle sampling for each recipe step.

    The entered global time count still describes the physical process. A
    cycle-integrated field has a separate minimum angular resolution, and
    identical source slots can reuse that field during accumulation.
    """
    if type(fast_cycle) is not bool:
        raise ValueError("快速算法开关必须为布尔值")
    if isinstance(fast_cycle_residual_percent, bool):
        raise ValueError("快速算法残余阈值须在 0–100% 之间")
    try:
        threshold_percent = float(fast_cycle_residual_percent)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("快速算法残余阈值须在 0–100% 之间") from exc
    if not math.isfinite(threshold_percent) or not 0 <= threshold_percent <= 100:
        raise ValueError("快速算法残余阈值须在 0–100% 之间")
    if isinstance(process, dict) and "profile" in process and "machine" in process:
        process = process["profile"]
    process = validate_process_profile(process)
    timing = process_timing(process, time_steps=settings.time_steps)
    revolutions = (settings.planet_teeth //
                   math.gcd(settings.sun_teeth, settings.planet_teeth))
    rpm = abs(settings.carrier_rpm)
    return_s = 60.0 * revolutions / rpm if rpm else None
    if return_s is not None and not math.isfinite(return_s):
        return_s = None
    cycle_count = None
    plans = []
    for duration_s, original_count in zip(
            timing["step_durations_s"], timing["step_time_steps"]):
        residual_s = None
        residual_fraction = None
        eligible = False
        if return_s is not None:
            ratio = duration_s / return_s
            nearest = round(ratio)
            # An exact integer number of revolutions can round just below its
            # integer in binary floating point. This tolerance applies only
            # to that numerical boundary, not to the user-defined threshold.
            if nearest >= 1 and math.isclose(
                    ratio, nearest, rel_tol=1e-12, abs_tol=1e-12):
                whole_cycles = nearest
                residual_s = 0.0
            else:
                whole_cycles = math.floor(ratio)
                residual_s = max(0.0, duration_s - whole_cycles * return_s)
            residual_fraction = residual_s / duration_s
            eligible = (whole_cycles >= 1 and
                        residual_fraction <= threshold_percent / 100.0)
        use_cycle = fast_cycle and eligible
        simulated_duration = return_s if use_cycle else duration_s
        if use_cycle:
            if cycle_count is None:
                cycle_count = max(
                    FAST_CYCLE_SAMPLES_PER_REVOLUTION * revolutions,
                    math.ceil(return_s / timing["time_step_s"]),
                )
            count = cycle_count
            sampling_capped = False
        elif fast_cycle:
            phase_count = math.ceil(
                FAST_CYCLE_SAMPLES_PER_REVOLUTION * rpm * duration_s / 60.0)
            requested_count = max(original_count, phase_count)
            count = requested_count
            sampling_capped = False
        else:
            count = original_count
            sampling_capped = False
        # Diagnostic only: keep the caller's exact sample count and physics.
        samples_per_revolution = (count * 60.0 / (rpm * simulated_duration)
                                  if rpm else None)
        angular_warning = (samples_per_revolution is not None and
                           samples_per_revolution < FAST_CYCLE_SAMPLES_PER_REVOLUTION)
        planet_ratio = 1 + settings.sun_teeth / settings.planet_teeth
        planet_samples = samples_per_revolution / planet_ratio if rpm else None
        local_dt = simulated_duration / count
        # Conservative substrate-point travel indicator, not projected mask
        # edge resolution or a source/grid convergence certificate.
        speed = 2 * math.pi * rpm / 60 * (settings.planet_offset_mm + planet_ratio * settings.substrate_radius_mm)
        grid_pitch = 2 * settings.substrate_radius_mm / (settings.grid_size - 1)
        plans.append({
            "samples_per_planet_revolution": planet_samples,
            "planet_phase_step_deg": 360 / planet_samples if rpm else 0.,
            "estimated_motion_grid_cells_per_step": speed * local_dt / grid_pitch,
            "below_planet_starting_heuristic": planet_samples is not None and planet_samples < 32,
            "samples_per_carrier_revolution": samples_per_revolution,
            "below_fast_angular_heuristic": angular_warning,
            "algorithm": "cycle" if use_cycle else "time",
            "time_steps": count,
            "simulated_duration_s": simulated_duration,
            "return_carrier_revolutions": revolutions,
            "return_cycle_s": return_s,
            "residual_s": residual_s,
            "residual_fraction": residual_fraction,
            "fast_cycle_eligible": eligible,
            "sampling_capped": sampling_capped,
        })
    return {
        "timing": timing, "steps": plans,
        "algorithm": "fast_cycle" if fast_cycle else "precise",
        "fast_cycle_residual_percent": threshold_percent,
        "return_carrier_revolutions": revolutions,
        "return_cycle_s": return_s,
        "cycle_time_steps": cycle_count,
    }


def sample_field_at(field, x_axis, y_axis, x_mm, y_mm):
    """Sample a substrate-local field by bilinear interpolation.

    At the circular rim, corners outside the sampled aperture are omitted and
    their remaining weights renormalized.  If an exact rim point has no valid
    interpolation corner, the closest valid grid sample is used.  Thus a rim
    value is an estimate, not an independently evaluated ray integral.
    """
    values = np.asarray(field, dtype=float)
    x_axis = np.asarray(x_axis, dtype=float)
    y_axis = np.asarray(y_axis, dtype=float)
    if values.ndim != 2 or values.shape != (len(y_axis), len(x_axis)):
        raise ValueError("厚度场与坐标轴尺寸不匹配")
    if len(x_axis) < 2 or len(y_axis) < 2:
        raise ValueError("厚度场坐标轴至少需要两个采样点")
    x = float(x_mm)
    y = float(y_mm)
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("采样点坐标必须是有限数值")
    if x < x_axis[0] - 1e-9 or x > x_axis[-1] + 1e-9 or \
            y < y_axis[0] - 1e-9 or y > y_axis[-1] + 1e-9:
        raise ValueError("采样点超出计算网格")
    x = min(max(x, x_axis[0]), x_axis[-1])
    y = min(max(y, y_axis[0]), y_axis[-1])
    i = min(max(int(np.searchsorted(x_axis, x, side="right")) - 1, 0), len(x_axis) - 2)
    j = min(max(int(np.searchsorted(y_axis, y, side="right")) - 1, 0), len(y_axis) - 2)
    wx = (x - x_axis[i]) / (x_axis[i + 1] - x_axis[i])
    wy = (y - y_axis[j]) / (y_axis[j + 1] - y_axis[j])
    corners = (
        (j, i, (1 - wx) * (1 - wy)),
        (j, i + 1, wx * (1 - wy)),
        (j + 1, i, (1 - wx) * wy),
        (j + 1, i + 1, wx * wy),
    )
    weighted = 0.0
    total_weight = 0.0
    for row, col, weight in corners:
        value = values[row, col]
        if np.isfinite(value):
            weighted += value * weight
            total_weight += weight
    if total_weight <= 0:
        valid_rows, valid_cols = np.where(np.isfinite(values))
        if not len(valid_rows):
            raise ValueError("基片中心附近没有有效的采样网格")
        distance2 = ((x_axis[valid_cols] - x) ** 2 +
                     (y_axis[valid_rows] - y) ** 2)
        closest = int(np.argmin(distance2))
        grid_pitch = max(abs(x_axis[1] - x_axis[0]),
                         abs(y_axis[1] - y_axis[0]))
        if distance2[closest] > (2 * grid_pitch) ** 2:
            raise ValueError("基片中心附近没有足够近的有效网格；请加密网格")
        return float(values[valid_rows[closest], valid_cols[closest]])
    return float(weighted / total_weight)


def _field_metrics(field_nm):
    values = np.asarray(field_nm, dtype=float)
    valid = values[np.isfinite(values)]
    if not len(valid):
        raise ValueError("累计厚度场没有有效采样点")
    mean = float(np.mean(valid))
    low = float(np.min(valid))
    high = float(np.max(valid))
    std = float(np.std(valid))
    return {
        "mean_nm": mean,
        "min_nm": low,
        "max_nm": high,
        "pv_nm": high - low,
        "cv_percent": std / mean * 100 if mean > 0 else 0.0,
        "pv_percent": (high - low) / mean * 100 if mean > 0 else 0.0,
        "min_percent": low / mean * 100 if mean > 0 else 0.0,
        "max_percent": high / mean * 100 if mean > 0 else 0.0,
    }


def _monitor_raw_flux(settings, source_profile, monitor, mask, cancel_event):
    """Integrate direct flux at a stationary, source-facing machine-frame sensor.

    The source is stationary too, so multiplying its weighted instantaneous
    flux by the deposition duration matches the simulator's midpoint-time
    integral units. The receiver faces downward, as the substrate does.
    """
    x = monitor["x_mm"]
    y = monitor["y_mm"]
    z = monitor["z_mm"]
    monitor_x = np.asarray([x], dtype=float)
    monitor_y = np.asarray([y], dtype=float)
    positions, weights = source_samples(settings, source_profile)
    exponent = effective_source_exponent(settings, source_profile)
    cover = source_cover(source_profile, settings)
    if cover and z <= settings.source_z_mm + cover["cover_height_mm"] + cover["cover_thickness_mm"]:
        raise ValueError("监测器须高于蒸发舟盖板顶部")
    parts = []
    for (source_x, source_y, source_z), source_weight in zip(positions, weights):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("计算已取消")
        sx = float(source_x)
        sy = float(source_y)
        sz = float(source_z)
        dz = z - sz
        if dz <= 0:
            # Sources emit toward +Z only.
            continue
        distance = math.hypot(x - sx, y - sy, dz)
        if math.isinf(distance):
            # Finite but extremely remote coordinates have zero representable
            # flux. The caller turns a zero reference into a clear error.
            continue
        try:
            inv_distance = 1.0 / distance
            flux = ((dz * inv_distance) ** (exponent + 1) *
                    inv_distance * inv_distance)
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("监测点通量无法用有限数表示；请调整监测器高度") from exc
        if not math.isfinite(flux):
            raise ValueError("监测点通量无法用有限数表示；请调整监测器位置")
        if flux <= 0:
            continue
        if cover is not None and not bool(source_visibility(
                np, monitor_x, monitor_y, z, sx, sy, sz, cover)[0]):
            continue
        if mask and bool(_blocked(
                np, monitor_x, monitor_y, mask, sx, sy, z, sz)[0]):
            continue
        if settings.beam_attenuation_m_inv:
            flux *= math.exp(-settings.beam_attenuation_m_inv * distance * 0.001)
        parts.append(float(source_weight) * flux)
    try:
        raw = settings.duration_s * math.fsum(parts)
    except OverflowError as exc:
        raise ValueError("监测点累计通量超出有限数范围") from exc
    if not math.isfinite(raw):
        raise ValueError("监测点累计通量超出有限数范围")
    return raw


def simulate_process(settings, process, *, process_base_dir=None, backend="自动",
                     cancel_event=None, fast_cycle=False,
                     fast_cycle_residual_percent=DEFAULT_FAST_CYCLE_RESIDUAL_PERCENT,
                     contribution_source_slot=None, progress=None, finite_periodic=False,
                     capture_layer_fields=False, layer_field_spool_path=None,
                     recipe_execution_algorithm=None,
                     recipe_execution_options=None):
    if layer_field_spool_path is None:
        return _simulate_process(settings, process, process_base_dir=process_base_dir,
            backend=backend, cancel_event=cancel_event, fast_cycle=fast_cycle,
            fast_cycle_residual_percent=fast_cycle_residual_percent,
            contribution_source_slot=contribution_source_slot, progress=progress,
            finite_periodic=finite_periodic, capture_layer_fields=capture_layer_fields,
            recipe_execution_algorithm=recipe_execution_algorithm,
            recipe_execution_options=recipe_execution_options)
    if capture_layer_fields is not True:
        raise ValueError("流式逐层场输出必须启用捕获选项")
    writer = _LayerFieldSpool(layer_field_spool_path)
    try:
        result = _simulate_process(settings, process, process_base_dir=process_base_dir,
            backend=backend, cancel_event=cancel_event, fast_cycle=fast_cycle,
            fast_cycle_residual_percent=fast_cycle_residual_percent,
            contribution_source_slot=contribution_source_slot, progress=progress,
            finite_periodic=finite_periodic, capture_layer_fields=True,
            recipe_execution_algorithm=recipe_execution_algorithm,
            recipe_execution_options=recipe_execution_options,
            _layer_field_writer=writer)
        writer.close()
        result["layer_field_spool_path"] = str(writer.path)
        return result
    except BaseException:
        writer.abort()
        raise


def _simulate_process(settings, process, *, process_base_dir=None, backend="自动",
                     cancel_event=None, fast_cycle=False,
                     fast_cycle_residual_percent=DEFAULT_FAST_CYCLE_RESIDUAL_PERCENT,
                     contribution_source_slot=None, progress=None, finite_periodic=False,
                      capture_layer_fields=False, _layer_field_writer=None,
                      recipe_execution_algorithm=None,
                      recipe_execution_options=None):
    """Resolve a process file's dependencies and accumulate only film thickness.

    ``process`` may be a v6 JSON object or a successful runtime returned by
    :func:`resolve_process_profile`. One-based slots retain machine file order.
    Motor phases advance continuously across sequential steps.
    """
    if type(capture_layer_fields) is not bool:
        raise ValueError("逐层场捕获选项必须为布尔值")
    if isinstance(process, dict) and "profile" in process and "machine" in process:
        runtime = process
        process = validate_process_profile(runtime["profile"])
        if runtime.get("errors"):
            raise ValueError("工艺依赖无效：\n" + "\n".join(runtime["errors"]))
        if runtime["machine"] is None:
            raise ValueError("工艺机器依赖尚未解析")
    else:
        process = validate_process_profile(process)
        if process_base_dir is None:
            raise ValueError("计算工艺须指定其文件目录 process_base_dir")
        runtime, errors = resolve_process_profile(process, process_base_dir)
        if errors:
            raise ValueError("工艺依赖无效：\n" + "\n".join(errors))
    machine = runtime["machine"]
    if machine.get("substrate_profile") is None or \
            machine.get("substrate_radius_mm") is None:
        raise ValueError("工艺基片文件尚未解析，无法计算")
    sources = machine["sources"]
    if contribution_source_slot is not None and (type(contribution_source_slot) is not int or
            not 1 <= contribution_source_slot <= len(sources)):
        raise ValueError("评分源位次须为机器中有效的1基整数")
    monitors = machine["monitors"]
    installed_masks = machine.get("masks", [])
    geometry = {key: machine[key] for key in _GEOMETRY_FIELDS}
    sampling = process_sampling_plan(
        replace(settings, **geometry), process, fast_cycle=fast_cycle,
        fast_cycle_residual_percent=fast_cycle_residual_percent)
    timing = sampling["timing"]
    accumulated = None
    first_result = None
    steps_out = []
    captured_layer_rows = []
    captured_layer_fields = {} if capture_layer_fields and _layer_field_writer is None else None
    layer_fields_digest = hashlib.sha256()
    dependency_sha256 = (_layer_dependency_sha256(
        process, machine, settings, sampling, backend=backend,
        fast_cycle=fast_cycle,
        fast_cycle_residual_percent=fast_cycle_residual_percent,
        finite_periodic=finite_periodic,
        contribution_source_slot=contribution_source_slot,
        recipe_execution_algorithm=recipe_execution_algorithm,
        recipe_execution_options=recipe_execution_options)
        if capture_layer_fields else None)
    layer_capture_size_checked = False
    devices = []
    notices = []
    elapsed_s = 0.0
    computed_time_steps = 0
    cycle_fields = {}
    from .periodic_integration import PeriodicCache, simulate_finite_periodic
    periodic_cache = PeriodicCache() if finite_periodic else None
    effective_settings = None

    for index, step in enumerate(process["steps"], 1):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("计算已取消")
        mask_slots = step_mask_slots(step, machine)
        step_id = _stable_layer_step_id(index, step) if capture_layer_fields else None
        mask = [installed_masks[slot]["profile"] for slot in mask_slots]
        if any(layer is None for layer in mask):
            raise ValueError(f"第 {index} 层的修正板文件已失效")
        source_slot = step["source_slot"]
        if not 1 <= source_slot <= len(sources):
            raise ValueError(f"第 {index} 层的源位次已失效")
        source = sources[source_slot - 1]
        source_profile = source.get("profile")
        if source_profile is None:
            raise ValueError(f"第 {index} 层的源文件已失效")
        environment = resolve_environment(process.get("environment"), step.get("environment"))
        environmental = environment_report(environment)
        duration_s = timing["step_durations_s"][index - 1]
        plan = sampling["steps"][index - 1]
        step_count = plan["time_steps"]
        carrier_degrees_per_s = settings.carrier_rpm * 6.0
        planet_degrees_per_s = carrier_degrees_per_s * (
            1 + geometry["sun_teeth"] / geometry["planet_teeth"])
        cycle_layer = plan["algorithm"] == "cycle"
        local_settings = replace(
            settings, **geometry, duration_s=plan["simulated_duration_s"],
            time_steps=step_count,
            carrier_phase_deg=(settings.carrier_phase_deg if cycle_layer else
                               settings.carrier_phase_deg +
                               carrier_degrees_per_s * elapsed_s) % 360.0,
            planet_phase_deg=(settings.planet_phase_deg if cycle_layer else
                              settings.planet_phase_deg +
                              planet_degrees_per_s * elapsed_s) % 360.0,
            source_x_mm=source["x_mm"], source_y_mm=source["y_mm"],
            source_z_mm=source["z_mm"],
            source_exponent=source_profile["source_exponent"],
            source_rate_nm_s=step["rate_nm_s"],
            beam_attenuation_m_inv=environmental["attenuation_coefficient_m_inv"],
        )
        if effective_settings is None:
            # The common record describes machine geometry and physical process
            # timing. Source-specific fields identify the first step; each step
            # below retains its actual source, phase and simulated time samples.
            effective_settings = asdict(replace(local_settings,
                duration_s=timing["total_duration_s"], time_steps=timing["time_steps"],
                carrier_phase_deg=settings.carrier_phase_deg,
                planet_phase_deg=settings.planet_phase_deg))
        cycle_key = (source_slot, mask_slots, source_rate_cache_key(source_profile, step["rate_nm_s"]),
                     json.dumps(environment, sort_keys=True, separators=(",", ":")))
        reused_cycle_field = cycle_layer and cycle_key in cycle_fields
        periodic_used = False
        if finite_periodic and not cycle_layer:
            result, actual_steps, periodic_used = simulate_finite_periodic(
                local_settings, mask=mask, source_profile=source_profile, backend=backend,
                cancel_event=cancel_event, period_s=sampling["return_cycle_s"],
                canonical_phases=(settings.carrier_phase_deg, settings.planet_phase_deg),
                cache=periodic_cache, identity=cycle_key,
                progress=(lambda fraction: progress({"fraction": ((index - 1) + fraction) / len(process["steps"]),
                    "step_index": index, "step_count": len(process["steps"]), "stage": "integrate"})) if progress else None)
            computed_time_steps += actual_steps
        elif reused_cycle_field:
            result = cycle_fields[cycle_key]
        else:
            result = simulate(local_settings, mask=mask, backend=backend,
                              cancel_event=cancel_event, source_profile=source_profile,
                              progress=(lambda fraction: progress({"fraction": ((index - 1) + fraction) / len(process["steps"]),
                                  "step_index": index, "step_count": len(process["steps"]), "stage": "integrate"})) if progress else None)
            computed_time_steps += step_count
            if cycle_layer:
                cycle_fields[cycle_key] = result
        field = result["field"]
        if first_result is None:
            first_result = result
            accumulated = np.full_like(field, np.nan, dtype=float)
            accumulated[np.isfinite(field)] = 0.0
        if capture_layer_fields and not layer_capture_size_checked:
            required_bytes = len(process["steps"]) * np.asarray(field).size * np.dtype(np.float64).itemsize
            if required_bytes > MAX_CAPTURED_LAYER_FIELD_BYTES:
                raise ValueError("逐层场捕获需要超过 256 MiB；请减小网格或步骤数")
            layer_capture_size_checked = True

        if step["target_mode"] == "mean":
            reference = float(np.nanmean(field))
        elif step["target_mode"] == "center":
            reference = sample_field_at(field, result["x"], result["y"], 0.0, 0.0)
        else:
            monitor_slot = step["monitor_slot"]
            if not 1 <= monitor_slot <= len(monitors):
                raise ValueError(f"第 {index} 层的监测器位次已失效")
            monitor = monitors[monitor_slot - 1]
            monitor_raw = _monitor_raw_flux(
                local_settings, source_profile, monitor, mask, cancel_event)
            reference = monitor_raw / result["mean_raw"]
        if not math.isfinite(reference) or reference <= 0:
            raise ValueError(
                f"第 {index} 层的厚度基准点没有有限的正沉积通量，无法按目标厚度归一化")
        scale_nm = step["target_nm"] / reference
        if not math.isfinite(scale_nm) or scale_nm <= 0:
            raise ValueError(f"第 {index} 层的厚度缩放超出有限数范围")
        valid = np.isfinite(field)
        with np.errstate(over="ignore", invalid="ignore"):
            step_field_nm = field * scale_nm
            if contribution_source_slot is None or source_slot == contribution_source_slot:
                accumulated[valid] += step_field_nm[valid]
        if not np.isfinite(step_field_nm[valid]).all() or not np.isfinite(accumulated[valid]).all():
            raise ValueError(f"第 {index} 层的厚度场超出有限数范围")

        if capture_layer_fields:
            domain = np.isfinite(accumulated)
            layer_values = np.full(accumulated.shape, np.nan, dtype=np.float64)
            layer_values[domain] = 0.0
            layer_valid = valid & domain
            layer_values[layer_valid] = np.asarray(step_field_nm, dtype=np.float64)[layer_valid]
            array_key = f"layer_field_{index:05d}"
            layer_metadata = _film_layer_metadata(process, step)
            layer_row = {
                "step_id": step_id, "step_index": index, "array_key": array_key,
                "included_in_total": (contribution_source_slot is None or
                                      source_slot == contribution_source_slot),
                "source_slot": source_slot, "target_nm": float(step["target_nm"]),
                **layer_metadata,
                "completed": True,
            }
            _update_layer_field_digest(layer_fields_digest, array_key, layer_values)
            if _layer_field_writer is None:
                captured_layer_fields[step_id] = layer_values
            else:
                _layer_field_writer.write(array_key, layer_values)
            captured_layer_rows.append(layer_row)
        if progress:
            progress({"fraction": index / len(process["steps"]), "step_index": index,
                      "step_count": len(process["steps"]), "stage": "integrate"})
        if "direct_beam_survival" in result:
            survival = float(result["direct_beam_survival"])
            environmental.update(direct_beam_survival=survival, vacuum_mean_raw=float(result["vacuum_mean_raw"]),
                attenuated_mean_raw=float(result["mean_raw"]),
                effective_optical_depth=-math.log(survival) if survival > 0 else None,
                collision_regime_warning=("Strong direct-beam loss: scattered flux omitted; not a complete deposition prediction" if survival < math.exp(-1) else None))
        summary = {
            "step_index": index, "source_slot": source_slot,
            **({"step_id": step_id} if capture_layer_fields else {}),
            **(_film_layer_metadata(process, step)
               if capture_layer_fields else {}),
            **({"environment": environmental} if environment else {}),
            "mask_slots": list(mask_slots),
            "effective_settings": asdict(local_settings),
            "mean_raw": float(result["mean_raw"]),
            "source_name": source_profile.get("name", f"源 {source_slot}"),
            "target_mode": step["target_mode"], "target_nm": step["target_nm"],
            "rate_nm_s": step["rate_nm_s"], "duration_s": duration_s,
            "time_steps": step_count,
            "time_step_s": plan["simulated_duration_s"] / step_count,
            "simulated_duration_s": plan["simulated_duration_s"],
            "algorithm": plan["algorithm"],
            "return_carrier_revolutions": plan["return_carrier_revolutions"],
            "return_cycle_s": plan["return_cycle_s"],
            "residual_s": plan["residual_s"],
            "residual_fraction": plan["residual_fraction"],
            "fast_cycle_eligible": plan["fast_cycle_eligible"],
            "sampling_capped": plan["sampling_capped"],
            "samples_per_carrier_revolution": plan["samples_per_carrier_revolution"],
            "samples_per_planet_revolution": plan["samples_per_planet_revolution"],
            "planet_phase_step_deg": plan["planet_phase_step_deg"],
            "estimated_motion_grid_cells_per_step": plan["estimated_motion_grid_cells_per_step"],
            "below_fast_angular_heuristic": plan["below_fast_angular_heuristic"],
            "reused_cycle_field": reused_cycle_field,
            "finite_periodic_integration": periodic_used,
            "reference_relative": reference, "scale_nm": scale_nm,
            "device": result["device"],
            "time_batch_steps": result.get("time_batch_steps"),
            "batch_elements_limit": result.get("batch_elements_limit"),
            **_field_metrics(step_field_nm),
        }
        if step["target_mode"] == "monitor":
            summary["monitor_slot"] = monitor_slot
            summary["monitor_name"] = monitor["name"]
            summary["monitor_kind"] = monitor["kind"]
            summary["monitor_x_mm"] = monitor["x_mm"]
            summary["monitor_y_mm"] = monitor["y_mm"]
            summary["monitor_z_mm"] = monitor["z_mm"]
        steps_out.append(summary)
        if result["device"] not in devices:
            devices.append(result["device"])
        notice = result.get("backend_notice")
        if notice and notice not in notices:
            notices.append(notice)
        elapsed_s += duration_s

    output = {
        "field_nm": accumulated, "x": first_result["x"],
        "y": first_result["y"], "device": "、".join(devices),
        "backend_notices": notices, "step_results": steps_out,
        "sampling_warnings": ([
            "部分膜层每公转采样低于快速模式的79点经验下限，可能发生时间混叠；"
            "请在同一工艺上加密时间采样比较。79点仅为项目经验规则，不是精度保证。"
        ] if any(p["below_fast_angular_heuristic"] for p in sampling["steps"]) else []),
        "total_target_nm": float(sum(step["target_nm"] for step in process["steps"]
                                     if contribution_source_slot is None or step["source_slot"] == contribution_source_slot)),
        "contribution_source_slot": contribution_source_slot,
        "total_duration_s": timing["total_duration_s"],
        "time_steps": timing["time_steps"], "time_step_s": timing["time_step_s"],
        "computed_time_steps": computed_time_steps,
        "algorithm": sampling["algorithm"],
        "fast_cycle_residual_percent": sampling["fast_cycle_residual_percent"],
        "return_carrier_revolutions": sampling["return_carrier_revolutions"],
        "return_cycle_s": sampling["return_cycle_s"],
        "requested_settings": asdict(settings),
        "effective_settings": effective_settings,
        **({"environment_model_notice": "仅零碰撞直达束；目标厚度归一化，未预测绝对净生长；基片/源温度、气体流量及反应化学未建模",
            "ballistic_layer_count": sum(item.get("environment", {}).get("transport_model") == "ballistic_loss" for item in steps_out),
            "minimum_direct_beam_survival": min((item["environment"]["direct_beam_survival"] for item in steps_out if "direct_beam_survival" in item.get("environment", {})), default=None),
            "strong_scattering_warning": any(item.get("environment", {}).get("collision_regime_warning") for item in steps_out)}
           if any("environment" in item for item in steps_out) else {}),
        "settings_scope": "process_common_geometry_and_physical_timing; source_fields_describe_first_step; step_results_hold_each_sampled_configuration",
        **_field_metrics(accumulated),
    }
    if capture_layer_fields:
        domain = np.isfinite(accumulated)
        if _layer_field_writer is None:
            summed = np.zeros(accumulated.shape, dtype=np.float64)
            for row in captured_layer_rows:
                if row["included_in_total"]:
                    values = captured_layer_fields[row["step_id"]]
                    summed[domain] += values[domain]
            if not np.allclose(summed[domain], accumulated[domain], rtol=1e-12, atol=1e-12):
                raise RuntimeError("逐层场之和未能闭合到本次过程总场")
        output["layer_field_manifest"] = {
            "schema_version": 1,
            "captured": True,
            "complete": True,
            "field_dtype": "float64",
            "field_shape": list(accumulated.shape),
            "field_units": "nm",
            "coordinates": {"frame": "substrate_local_xy", "x_array": "x",
                            "y_array": "y", "unit": "mm"},
            "material_coordinate": {"axis": "substrate_normal", "origin": "original_substrate_front",
                                    "positive_direction": "outward", "unit": "nm"},
            "layer_value_semantics": "per_step_growth_increment; cumulative_ordered_sum_is_interface_offset",
            "valid_mask": {"semantics": "finite_nodes_are_valid; outside_domain_is_nan",
                           "array_source": "layer_field_*"},
            "sequence_order": "process_recipe_order",
            "stack_order": "first_step_is_substrate_adjacent_then_later_steps_overcoat",
            "contributor_semantics": "included_in_total_field",
            "dependency_sha256": dependency_sha256,
            "fields_sha256": layer_fields_digest.hexdigest(),
            "contribution_source_slot": contribution_source_slot,
            "fields": captured_layer_rows,
        }
        if _layer_field_writer is None:
            output["layer_fields"] = captured_layer_fields
    return output
