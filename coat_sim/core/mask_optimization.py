"""Bounded correction-board outline search using the complete process model.

The response scan evaluates small candidate polygons against the final film
field. It is a search initializer, not a linear shadow-weight matrix. Every
accepted outline is scored with ``simulate_process`` so source quadrature,
per-step board selections, the second board, thickness normalization, and
the selected time integrator remain in the objective.

From-scratch mode searches one or two hole-free opaque star-shaped splines.
Loaded mode deforms the original polygon vertices without an initial refit.
Fixed boards, including their holes, remain untouched in both modes. Heights
are fixed throughout. This is an exploratory bounded search rather than a
manufacturing or optical-performance guarantee.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
import math
import time

import numpy as np

from .machine_profiles import validate_mask_profile
from .process_profiles import validate_process_profile, step_mask_slots
from .process_simulator import simulate_process
from .simulator import Settings

SEARCH_SHORTLIST_SIZE = 4
OUTLINE_SAMPLES_PER_CONTROL = 8
MIN_OUTLINE_SAMPLES_PER_CONTROL = 3


@dataclass
class MaskOptimizationResult:
    """The report and board designs are JSON-ready; simulation keeps arrays."""

    report: dict
    candidate_masks: list[dict]
    simulation: dict
    original_simulation: dict | None = None


@dataclass(frozen=True)
class _BoardBounds:
    slot: int
    x_min: float
    x_max: float
    y_min: float
    y_max: float

    @property
    def width(self) -> float:
        return self.x_max - self.x_min

    @property
    def height(self) -> float:
        return self.y_max - self.y_min


class _CallbackCancel:
    def __init__(self, callback: Callable[[], bool] | None):
        self.callback = callback

    def is_set(self) -> bool:
        return bool(self.callback is not None and self.callback())


def _number(value, label, *, low=None, high=None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须为有限数值")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须为有限数值") from exc
    if not math.isfinite(number) or (low is not None and number < low) or \
            (high is not None and number > high):
        raise ValueError(f"{label}超出有效范围")
    return number


def _integer(value, label, low, high) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label}必须为 {low}–{high} 的整数")
    return value


def _options(settings: Settings, machine: Mapping, supplied: Mapping) -> dict:
    if not isinstance(supplied, Mapping):
        raise ValueError("修正板优化参数必须是对象")
    mode = supplied.get("mode", "optimize_loaded")
    if mode not in ("auto_design", "optimize_loaded", "fixed_plus_variable"):
        raise ValueError("未知修正板优化模式")
    initialization = supplied.get("variable_mode", "auto_design") if mode == "fixed_plus_variable" else mode
    if initialization not in ("auto_design", "optimize_loaded"):
        raise ValueError("可变板须选择从零设计或从已载入轮廓优化")
    slots = supplied.get("mask_slots")
    if not isinstance(slots, (list, tuple)) or not slots or \
            any(type(slot) is not int for slot in slots) or len(set(slots)) != len(slots):
        raise ValueError("优化板位次须为非空且不重复的 0 基整数列表")
    if any(slot < 0 or slot >= len(machine["masks"]) for slot in slots):
        raise ValueError("优化板位次不在机器已安装修正板范围内")
    slots = tuple(sorted(slots))
    fixed = supplied.get("fixed_mask_slots", [])
    if not isinstance(fixed, (list, tuple)) or any(type(slot) is not int or not 0 <= slot < len(machine["masks"]) for slot in fixed):
        raise ValueError("固定板须使用已载入的0基整数板位次")
    if mode == "fixed_plus_variable":
        if len(fixed) != 1 or len(slots) != 1 or set(fixed).intersection(slots):
            raise ValueError("固定板模式须选择一块固定板与另一块可变板")
    elif fixed:
        raise ValueError("fixed_mask_slots 仅用于固定板模式")
    raw_bounds = supplied.get("mask_bounds")
    if not isinstance(raw_bounds, (list, tuple)):
        raise ValueError("每块优化板都须提供 mask_bounds 设计包络")
    bounds = {}
    for entry in raw_bounds:
        if not isinstance(entry, Mapping) or type(entry.get("slot")) is not int:
            raise ValueError("mask_bounds 每项须包含 0 基 slot")
        slot = entry["slot"]
        if slot in bounds or slot not in slots:
            raise ValueError("mask_bounds 板位次重复或不在优化板内")
        x_min = _number(entry.get("x_min_mm"), "设计包络 X 下界")
        x_max = _number(entry.get("x_max_mm"), "设计包络 X 上界")
        y_min = _number(entry.get("y_min_mm"), "设计包络 Y 下界")
        y_max = _number(entry.get("y_max_mm"), "设计包络 Y 上界")
        if not x_min < x_max or not y_min < y_max:
            raise ValueError("设计包络的 X/Y 上界必须大于下界")
        bounds[slot] = _BoardBounds(slot, x_min, x_max, y_min, y_max)
    if set(bounds) != set(slots):
        raise ValueError("每块优化板都须各有一项 mask_bounds")

    objective = supplied.get("objective", {"kind": "uniformity"})
    if not isinstance(objective, Mapping):
        raise ValueError("objective 必须是对象")
    objective_kind = objective.get("kind", "uniformity")
    if objective_kind not in ("uniformity", "target_map"):
        raise ValueError("目标模式须为 uniformity 或 target_map")
    target_map = None
    if objective_kind == "target_map":
        try:
            target_map = np.asarray(objective.get("target_field_nm"), dtype=float)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("目标膜厚图必须是数值网格") from exc
        if target_map.shape != (settings.grid_size, settings.grid_size):
            raise ValueError("目标膜厚图须与原始计算网格边长一致")
        finite = np.isfinite(target_map)
        if not np.any(finite) or np.any(target_map[finite] <= 0):
            raise ValueError("目标膜厚图有效区域须为正的有限 nm 值")

    backend = str(supplied.get("backend", "自动")).strip()
    backend = {"auto": "自动", "cpu": "CPU", "gpu": "GPU",
               "directml": "DirectML"}.get(backend.lower(), backend)
    if backend not in ("自动", "CPU", "GPU", "DirectML"):
        raise ValueError("优化计算设备须为 自动、CPU、GPU 或 DirectML")
    fast_cycle = supplied.get("fast_cycle", False)
    if type(fast_cycle) is not bool:
        raise ValueError("fast_cycle 必须是布尔值")
    threshold = _number(supplied.get("fast_cycle_residual_percent", 5.0),
                        "快速算法残余阈值", low=0.0, high=100.0)
    if threshold <= 0:
        raise ValueError("快速算法残余阈值须大于0且不超过100%")
    search_grid = _integer(supplied.get("search_grid_size",
                                        min(81, settings.grid_size)),
                           "搜索网格边长", 21, settings.grid_size)
    budget = _integer(supplied.get("max_evaluations", 48),
                      "最大评分次数", 4, 2000)
    seed_grid = _integer(supplied.get("seed_grid_points", 2),
                         "初形扫描每轴点数", 1, 5)
    if initialization == "auto_design" and budget < 2 + len(slots) * seed_grid ** 2:
        raise ValueError("最大评分次数不足以完成基线和各板初形扫描")
    controls = _integer(supplied.get("control_points", 8),
                        "轮廓控制点数", 6, 24)
    min_radius = _number(supplied.get("min_radial_extent_mm", 0.0),
                         "最小径向尺寸", low=0.0)
    min_edge = _number(supplied.get("min_edge_length_mm", 0.0),
                       "最小轮廓边长", low=0.0)
    max_area_fraction = _number(supplied.get("max_area_fraction", 1.0),
                                "最大包络遮挡面积比例", low=0.0, high=1.0)
    if max_area_fraction <= 0:
        raise ValueError("最大包络遮挡面积比例须大于 0")
    initial_radius = supplied.get("initial_radius_mm")
    if initial_radius is not None:
        initial_radius = _number(initial_radius, "初始轮廓半径", low=0.0)
        if initial_radius <= 0:
            raise ValueError("初始轮廓半径须大于 0")
    return {
        "mode": mode, "initialization": initialization,
        "fixed_mask_slots": tuple(fixed),
        "new_mask_slots": tuple(supplied.get("new_mask_slots", [])),
        "mask_slots": slots, "bounds": bounds, "source_slot": supplied.get("source_slot"),
        "objective_kind": objective_kind, "target_map": target_map,
        "backend": backend, "fast_cycle": fast_cycle,
        "fast_cycle_residual_percent": threshold,
        "search_grid_size": search_grid, "max_evaluations": budget,
        "seed_grid_points": seed_grid, "control_points": controls,
        "min_radial_extent_mm": min_radius,
        "min_edge_length_mm": min_edge,
        "max_area_fraction": max_area_fraction,
        "initial_radius_mm": initial_radius,
    }


def _resample_target(target: np.ndarray, new_size: int) -> np.ndarray:
    """Weighted bilinear resize so NaN outside the aperture stays missing."""
    if target.shape[0] == new_size:
        return target.copy()
    old_axis = np.linspace(0.0, 1.0, target.shape[0])
    new_axis = np.linspace(0.0, 1.0, new_size)
    finite = np.isfinite(target)
    weighted = np.where(finite, target, 0.0)
    weight = finite.astype(float)

    def interpolate(values):
        rows = np.stack([np.interp(new_axis, old_axis, row) for row in values])
        return np.stack([np.interp(new_axis, old_axis, column)
                         for column in rows.T], axis=1)

    numerator = interpolate(weighted)
    denominator = interpolate(weight)
    resized = np.full((new_size, new_size), np.nan)
    valid = denominator > 1e-12
    resized[valid] = numerator[valid] / denominator[valid]
    return resized


def _quality(result: Mapping, objective_kind: str,
             target: np.ndarray | None) -> dict:
    field = np.asarray(result["field_nm"], dtype=float)
    valid = np.isfinite(field)
    if target is not None:
        valid &= np.isfinite(target)
    if not np.any(valid):
        raise ValueError("目标图与有效基片网格没有交集")
    values = field[valid]
    mean = float(np.mean(values))
    cv = float(np.std(values) / mean * 100) if mean > 0 else math.inf
    if objective_kind == "uniformity":
        score = cv / 100.0
        rmse_nm = float(np.std(values))
    else:
        goals = target[valid]
        scale = float(np.mean(goals))
        rmse_nm = float(np.sqrt(np.mean((values - goals) ** 2)))
        score = rmse_nm / scale
    if not math.isfinite(score):
        raise ValueError("候选修正板的膜厚目标无法计算")
    return {"score": score, "mean_nm": mean, "cv_percent": cv,
            "pv_nm": float(np.max(values) - np.min(values)),
            "rmse_nm": rmse_nm, "valid_points": int(len(values))}


def _outline(center_x: float, center_y: float,
             radii: tuple[float, ...], *, min_edge_length: float = 0.0) -> list[list[float]]:
    """Sample the same periodic cubic radial spline used throughout the search.

    Positive B-spline weights preserve positive radii. The saved polygon is a
    discretization of this smooth curve, not a smoothing pass after scoring.
    A requested minimum polygon edge length reduces tessellation density.
    """
    controls = len(radii)
    minimum_count = controls * MIN_OUTLINE_SAMPLES_PER_CONTROL
    for count in range(controls * OUTLINE_SAMPLES_PER_CONTROL, minimum_count - 1, -1):
        points = []
        for index in range(count):
            position = controls * index / count
            knot = int(position)
            u = position - knot
            weights = ((1 - u) ** 3 / 6,
                       (3 * u ** 3 - 6 * u ** 2 + 4) / 6,
                       (-3 * u ** 3 + 3 * u ** 2 + 3 * u + 1) / 6,
                       u ** 3 / 6)
            radius = math.fsum(weights[offset] * radii[(knot + offset - 1) % controls]
                               for offset in range(4))
            angle = 2 * math.pi * index / count
            points.append([center_x + radius * math.cos(angle),
                           center_y + radius * math.sin(angle)])
        if all(math.hypot(points[index][0] - points[index - 1][0],
                          points[index][1] - points[index - 1][1]) + 1e-12 >= min_edge_length
               for index in range(count)):
            return points
    return []


def _polygon_area(points: list[list[float]]) -> float:
    return abs(math.fsum(
        points[i - 1][0] * points[i][1] -
        points[i][0] * points[i - 1][1]
        for i in range(len(points)))) / 2.0


def _profile_from_state(state: tuple, z_mm: float, bound: _BoardBounds,
                        config: Mapping) -> dict | None:
    cx, cy, radii = state
    if not bound.x_min <= cx <= bound.x_max or \
            not bound.y_min <= cy <= bound.y_max or \
            any(radius < config["min_radial_extent_mm"] or radius <= 0
                for radius in radii):
        return None
    outer = _outline(cx, cy, radii, min_edge_length=config["min_edge_length_mm"])
    if not outer:
        return None
    if any(not bound.x_min <= x <= bound.x_max or
           not bound.y_min <= y <= bound.y_max for x, y in outer):
        return None
    if any(math.hypot(outer[i][0] - outer[i - 1][0],
                      outer[i][1] - outer[i - 1][1]) + 1e-12 <
           config["min_edge_length_mm"] for i in range(len(outer))):
        return None
    if _polygon_area(outer) > (config["max_area_fraction"] *
                               bound.width * bound.height + 1e-9):
        return None
    try:
        return validate_mask_profile({"z_mm": z_mm,
                                      "outer": outer, "holes": []})
    except (ValueError, TypeError):
        return None


def _loaded_profile_from_state(state, original, bound, config):
    """Deform loaded vertices by a positive periodic cubic radial scale.

    The mean vertex center and unit scales reproduce every original coordinate
    exactly. Translations and smooth angular scale controls are searched using
    the same complete-physics objective as the from-scratch spline design.
    """
    points = np.asarray(original["outer"], dtype=float)
    center = np.mean(points, axis=0)
    cx, cy, scales = state
    if any(value <= 0 or not math.isfinite(value) for value in scales):
        return None
    if cx == center[0] and cy == center[1] and all(value == 1.0 for value in scales):
        profile = deepcopy(original)
        outer = profile["outer"]
    else:
        outer = []
        for point in points:
            delta = point - center
            position = ((math.atan2(delta[1], delta[0]) % (2 * math.pi)) / (2 * math.pi)) * len(scales)
            knot = int(position)
            u = position - knot
            weights = ((1-u)**3/6, (3*u**3-6*u*u+4)/6, (-3*u**3+3*u*u+3*u+1)/6, u**3/6)
            scale = math.fsum(weights[offset] * scales[(knot + offset - 1) % len(scales)] for offset in range(4))
            outer.append([float(cx + delta[0] * scale), float(cy + delta[1] * scale)])
        profile = {**deepcopy(original), "outer": outer}
    if any(not bound.x_min <= x <= bound.x_max or not bound.y_min <= y <= bound.y_max for x, y in outer):
        return None
    if any(math.hypot(x-cx, y-cy) < config["min_radial_extent_mm"] for x, y in outer):
        return None
    if any(math.dist(outer[i], outer[i-1]) + 1e-12 < config["min_edge_length_mm"] for i in range(len(outer))):
        return None
    if _polygon_area(outer) > config["max_area_fraction"] * bound.width * bound.height + 1e-9:
        return None
    try:
        validate_mask_profile(profile)
    except (ValueError, TypeError):
        return None
    return profile


def _runtime(recipe: Mapping, machine: Mapping,
             boards: Mapping[int, dict]) -> dict:
    local_machine = deepcopy(machine)
    for slot, profile in boards.items():
        local_machine["masks"][slot]["profile"] = deepcopy(profile)
    return {"profile": recipe, "machine": local_machine, "errors": []}


def _without_selected_boards(recipe: Mapping, selected: set[int]) -> dict:
    baseline = deepcopy(recipe)
    for step in baseline["steps"]:
        step["mask_slots"] = [slot for slot in step["mask_slots"]
                              if slot not in selected]
    return baseline


def optimize_correction_boards(
    settings: Settings, recipe: Mapping, machine: Mapping, options: Mapping,
    *, progress_callback: Callable[[dict], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> MaskOptimizationResult:
    """Search fixed-height board outlines and verify on the full input grid.

    ``machine`` is a resolved runtime machine with all installed board slots,
    loaded source/board profiles and the selected substrate profile. Process
    steps must carry zero-based ``mask_slots``. Uniformity always scores the
    complete film stack; an explicit source target map scores that contribution.
    ``max_evaluations`` counts search-grid simulations including two baselines.
    Verification retains four search candidates and, in loaded mode, the
    original boards; it adds at most five full-grid simulations when grids differ.
    Auto-design ignores loaded geometry as an initializer or fallback. Loaded
    mode starts at the exact input vertices with unit angular radial scales.
    """
    if not isinstance(settings, Settings):
        raise ValueError("settings 必须是 Settings")
    if isinstance(recipe, Mapping) and "profile" in recipe:
        recipe = recipe["profile"]
    recipe = validate_process_profile(recipe)
    if not isinstance(machine, Mapping) or not isinstance(machine.get("masks"), list):
        raise ValueError("machine 必须是已解析的完整机器结构")
    if machine.get("substrate_profile") is None or machine.get("substrate_radius_mm") is None:
        raise ValueError("机器运行结构缺少已解析的工艺基片")
    for step in recipe["steps"]:
        step["mask_slots"] = list(step_mask_slots(step, machine))
    config = _options(settings, machine, options)
    slots = config["mask_slots"]
    if set(slots).intersection(config["fixed_mask_slots"]):
        raise ValueError("固定板不能同时作为优化变量")
    for slot in config["fixed_mask_slots"]:
        if not 0 <= slot < len(machine["masks"]) or machine["masks"][slot].get("profile") is None:
            raise ValueError("固定板必须是已载入的有效轮廓")
    for step in recipe["steps"]:
        if set(step["mask_slots"]).intersection(slots) and not set(config["fixed_mask_slots"]).issubset(step["mask_slots"]):
            raise ValueError("固定板+可变板膜层必须同时启用两块板")
    for slot in slots:
        board = machine["masks"][slot]
        if not isinstance(board, Mapping) or board.get("profile") is None:
            raise ValueError(f"第 {slot + 1} 块修正板依赖尚未解析")
        if config["initialization"] == "optimize_loaded" and board["profile"].get("holes"):
            raise ValueError(
                f"第 {slot + 1} 块修正板包含孔洞；首版只优化无孔外轮廓，"
                "请先另存无孔板设计。未选板的孔洞会保留。")
    if not any(set(step["mask_slots"]) & set(slots) for step in recipe["steps"]):
        raise ValueError("所选修正板未在任何膜层启用，无法优化膜厚")

    started = time.perf_counter()
    cancel = _CallbackCancel(cancel_callback)
    search_settings = replace(settings, grid_size=config["search_grid_size"])
    target_search = (_resample_target(config["target_map"],
                                      config["search_grid_size"])
                     if config["target_map"] is not None else None)
    score_source = (config.get("source_slot")
                    if config["objective_kind"] == "target_map" else None)
    score_scope = "source_contribution" if score_source is not None else "full_recipe"
    durations = []
    evaluations = 0
    best_score = math.inf
    best_quality = None
    best_masks = None
    shortlist = []
    verification_evaluations = 0
    verification_max_evaluations = SEARCH_SHORTLIST_SIZE + 1
    verified_best_quality = None
    response_samples = []
    generated_profiles = set()
    original_masks = {
        slot: deepcopy(machine["masks"][slot]["profile"]) for slot in slots}

    def publish(stage: str) -> None:
        if progress_callback is None:
            return
        mean_duration = float(np.median(durations)) if durations else None
        verify_factor = ((settings.grid_size / config["search_grid_size"]) ** 2
                         if settings.grid_size != config["search_grid_size"] else 0.0)
        remaining = (mean_duration *
                     (max(0, config["max_evaluations"] - evaluations) +
                      verification_max_evaluations * verify_factor)
                     if mean_duration is not None else None)
        if stage == "verify":
            remaining = (mean_duration * verify_factor *
                         max(0, verification_max_evaluations - verification_evaluations)
                         if mean_duration is not None else None)
        if stage == "complete":
            remaining = 0.0
        progress_callback({
            "stage": stage, "evaluations": evaluations,
            "max_evaluations": config["max_evaluations"],
            "best_score": best_score if math.isfinite(best_score) else None,
            "best_search_score": best_score if math.isfinite(best_score) else None,
            "best_search_cv_percent": best_quality["cv_percent"] if best_quality else None,
            "best_verified_score": verified_best_quality["score"] if verified_best_quality else None,
            "best_verified_cv_percent": verified_best_quality["cv_percent"] if verified_best_quality else None,
            "objective": config["objective_kind"], "score_scope": score_scope,
            "search_grid_size": config["search_grid_size"],
            "verification_grid_size": settings.grid_size,
            "verification_evaluations": verification_evaluations,
            "verification_max_evaluations": verification_max_evaluations,
            "elapsed_s": time.perf_counter() - started,
            "estimated_remaining_s": remaining,
            "estimate_basis": "已完成评分的实测中位用时" if durations else None,
        })

    def feasible_boards(boards):
        # Original polygons enter the best set only when every requested
        # constraint can be checked; generated radial outlines are checked
        # by _profile_from_state before evaluation.
        for slot in slots:
            profile = boards[slot]
            points = np.asarray(profile["outer"], dtype=float)
            bound = config["bounds"][slot]
            if (np.any(points[:, 0] < bound.x_min - 1e-9) or
                    np.any(points[:, 0] > bound.x_max + 1e-9) or
                    np.any(points[:, 1] < bound.y_min - 1e-9) or
                    np.any(points[:, 1] > bound.y_max + 1e-9)):
                return False
            edges = np.linalg.norm(np.roll(points, -1, axis=0) - points, axis=1)
            if np.any(edges + 1e-9 < config["min_edge_length_mm"]):
                return False
            area = abs(float(np.sum(points[:, 0] * np.roll(points[:, 1], -1) -
                                    points[:, 1] * np.roll(points[:, 0], -1)))) / 2
            if area > config["max_area_fraction"] * bound.width * bound.height + 1e-9:
                return False
            if (config["min_radial_extent_mm"] > 0 and
                    json_key(profile) not in generated_profiles):
                if config["initialization"] != "optimize_loaded" or np.any(
                        np.linalg.norm(points - np.mean(points, axis=0), axis=1) < config["min_radial_extent_mm"]):
                    return False
        return True

    def json_key(profile):
        return (float(profile["z_mm"]), tuple(tuple(point) for point in profile["outer"]))

    def evaluate(boards: Mapping[int, dict], *, stage: str,
                 trial_recipe: Mapping | None = None,
                 allow_infeasible: bool = False):
        nonlocal evaluations, best_score, best_quality, best_masks
        if cancel.is_set():
            raise InterruptedError("修正板优化已取消")
        if evaluations >= config["max_evaluations"]:
            return None
        publish(stage)
        tic = time.perf_counter()
        try:
            result = simulate_process(
                search_settings,
                _runtime(recipe if trial_recipe is None else trial_recipe,
                         machine, boards),
                backend=config["backend"], cancel_event=cancel,
                contribution_source_slot=score_source,
                fast_cycle=config["fast_cycle"],
                fast_cycle_residual_percent=config[
                    "fast_cycle_residual_percent"])
            quality = _quality(result, config["objective_kind"], target_search)
        except ValueError:
            if not allow_infeasible:
                raise
            result = quality = None
        durations.append(time.perf_counter() - tic)
        evaluations += 1
        eligible = config["initialization"] == "optimize_loaded" or stage != "current"
        if eligible and trial_recipe is None and quality is not None and feasible_boards(boards):
            identity = tuple((slot, json_key(boards[slot])) for slot in slots)
            if not any(item["identity"] == identity for item in shortlist):
                shortlist.append({"identity": identity, "masks": deepcopy(boards),
                                  "quality": quality, "result": result,
                                  "duration": durations[-1]})
                shortlist.sort(key=lambda item: item["quality"]["score"])
                del shortlist[SEARCH_SHORTLIST_SIZE:]
            if quality["score"] < best_score:
                best_score = quality["score"]
                best_quality = quality
                best_masks = deepcopy(boards)
        publish(stage)
        return result, quality

    baseline_recipe = _without_selected_boards(recipe, set(slots))
    baseline_result, baseline_quality = evaluate(
        original_masks, stage="baseline", trial_recipe=baseline_recipe)
    current_result, current_quality = evaluate(
        original_masks, stage="current", allow_infeasible=True,
        trial_recipe=baseline_recipe if set(slots).intersection(config["new_mask_slots"]) else None)
    loaded = config["initialization"] == "optimize_loaded"

    def profile_from_state(slot, state):
        if loaded:
            return _loaded_profile_from_state(state, original_masks[slot], config["bounds"][slot], config)
        return _profile_from_state(state, float(original_masks[slot]["z_mm"]), config["bounds"][slot], config)
    # Start each response scan with every selected board in a feasible,
    # minimally sized outline. Keeping a different board's original outline
    # could block all flux and falsely make the first scan appear infeasible.
    if not loaded:
        working = {}
        states = {}
        initial_radii = {}
        for slot in slots:
            bound = config["bounds"][slot]
            radius = (config["initial_radius_mm"] if
                      config["initial_radius_mm"] is not None else
                      0.1 * min(bound.width, bound.height))
            angle = math.pi / (config["control_points"] * MIN_OUTLINE_SAMPLES_PER_CONTROL)
            minimum = max(config["min_radial_extent_mm"],
                          config["min_edge_length_mm"] / (2 * math.sin(angle)), 1e-6)
            area_limit = math.sqrt(config["max_area_fraction"] * bound.width * bound.height / math.pi)
            maximum = min(min(bound.width, bound.height) / 2, area_limit)
            if maximum < minimum:
                raise ValueError(f"第 {slot + 1} 块板的初始半径与设计包络不相容")
            radius = max(minimum, min(max(radius, minimum), maximum * (1 - 1e-10)))
            z_mm = float(original_masks[slot]["z_mm"])
            state = ((bound.x_min + bound.x_max) / 2,
                     (bound.y_min + bound.y_max) / 2,
                     tuple(radius for _ in range(config["control_points"])))
            profile = _profile_from_state(state, z_mm, bound, config)
            if profile is None:
                raise ValueError(f"第 {slot + 1} 块板的包络/面积/边长限制没有可行初形")
            states[slot] = state
            initial_radii[slot] = radius
            working[slot] = profile
            generated_profiles.add(json_key(profile))

        working_quality = None
        for slot in slots:
            if cancel.is_set():
                raise InterruptedError("修正板优化已取消")
            bound = config["bounds"][slot]
            radius = initial_radii[slot]
            z_mm = float(original_masks[slot]["z_mm"])
            seed_state = None
            seed_quality = None
            seed_profile = None
            x_centers = np.linspace(bound.x_min + radius,
                                    bound.x_max - radius,
                                    config["seed_grid_points"])
            y_centers = np.linspace(bound.y_min + radius,
                                    bound.y_max - radius,
                                    config["seed_grid_points"])
            if config["seed_grid_points"] == 1:
                x_centers = [(bound.x_min + bound.x_max) / 2]
                y_centers = [(bound.y_min + bound.y_max) / 2]
            for cy in y_centers:
                for cx in x_centers:
                    state = (float(cx), float(cy),
                             tuple(radius for _ in range(config["control_points"])))
                    profile = _profile_from_state(state, z_mm, bound, config)
                    if profile is None:
                        continue
                    generated_profiles.add(json_key(profile))
                    candidate = dict(working)
                    candidate[slot] = profile
                    output = evaluate(candidate, stage="response",
                                      allow_infeasible=True)
                    if output is None:
                        break
                    result, quality = output
                    if quality is None:
                        continue
                    shared = np.isfinite(result["field_nm"]) & \
                             np.isfinite(baseline_result["field_nm"])
                    response_rms = (float(np.sqrt(np.mean((
                        result["field_nm"][shared] -
                        baseline_result["field_nm"][shared]) ** 2)))
                        if np.any(shared) else None)
                    response_samples.append({
                        "slot": slot, "center_x_mm": float(cx),
                        "center_y_mm": float(cy),
                        "score": quality["score"],
                        "improvement_vs_no_selected_board":
                            baseline_quality["score"] - quality["score"],
                        "response_rms_nm": response_rms,
                    })
                    if seed_quality is None or quality["score"] < seed_quality["score"]:
                        seed_state, seed_quality = state, quality
                        seed_profile = profile
                if evaluations >= config["max_evaluations"]:
                    break
            if seed_state is None:
                raise ValueError(f"第 {slot + 1} 块板的包络/面积/边长限制没有可行初形")
            states[slot] = seed_state
            working[slot] = seed_profile
            working_quality = seed_quality

    else:
        # Exactly reproduce the loaded polygon at unit radial scales. Unlike a
        # fresh circle scan this makes each accepted perturbation descend from
        # the user's actual contour, with no initial fitting/tessellation loss.
        states = {}
        working = deepcopy(original_masks)
        for slot in slots:
            points = np.asarray(original_masks[slot]["outer"], dtype=float)
            center = np.mean(points, axis=0)
            states[slot] = (float(center[0]), float(center[1]), tuple(1.0 for _ in range(config["control_points"])))
            if profile_from_state(slot, states[slot]) is None:
                raise ValueError(f"第 {slot + 1} 块已载入轮廓不满足设计包络或几何限制；请调整限制或选择从零设计")
            generated_profiles.add(json_key(original_masks[slot]))
        working_quality = current_quality
        if working_quality is None:
            raise ValueError("已载入轮廓无法计算；请选择从零设计或修正原板")

    # Project-defined bounded coordinate search. Geometry and final thickness
    # are evaluated anew for each attempted center or radial-control change.
    center_steps = {slot: 0.1 * min(config["bounds"][slot].width,
                                   config["bounds"][slot].height)
                    for slot in slots}
    radial_steps = {slot: max(0.2 * sum(states[slot][2]) /
                              len(states[slot][2]), 1e-6)
                    for slot in slots}
    while evaluations < config["max_evaluations"]:
        improved = False
        for slot in slots:
            bound = config["bounds"][slot]
            z_mm = float(original_masks[slot]["z_mm"])
            for variable in range(config["control_points"] + 2):
                if evaluations >= config["max_evaluations"]:
                    break
                step_size = (center_steps[slot] if variable < 2 else
                             radial_steps[slot])
                for direction in (-1.0, 1.0):
                    if evaluations >= config["max_evaluations"]:
                        break
                    old_state = states[slot]
                    values = [old_state[0], old_state[1], *old_state[2]]
                    values[variable] += direction * step_size
                    trial_state = (values[0], values[1], tuple(values[2:]))
                    profile = profile_from_state(slot, trial_state)
                    if profile is None:
                        continue
                    generated_profiles.add(json_key(profile))
                    candidate = dict(working)
                    candidate[slot] = profile
                    output = evaluate(candidate, stage="optimize",
                                      allow_infeasible=True)
                    if output is None:
                        break
                    _, quality = output
                    if quality is not None and \
                            quality["score"] + 1e-12 < working_quality["score"]:
                        states[slot] = trial_state
                        working[slot] = profile
                        working_quality = quality
                        improved = True
        if not improved:
            for slot in slots:
                center_steps[slot] *= 0.5
                radial_steps[slot] *= 0.5
            if all(center_steps[slot] < 1e-4 and
                   radial_steps[slot] < 1e-4 for slot in slots):
                break

    if best_masks is None or best_quality is None:
        raise ValueError("修正板优化没有得到有效膜厚结果")
    if cancel.is_set():
        raise InterruptedError("修正板优化已取消")
    verification_started = time.perf_counter()
    search_best_masks = deepcopy(best_masks)
    candidates = list(shortlist)
    original_identity = tuple((slot, json_key(original_masks[slot])) for slot in slots)
    if loaded and current_quality is not None and not any(item["identity"] == original_identity for item in candidates):
        candidates.append({"identity": original_identity, "masks": original_masks,
                           "quality": current_quality, "result": current_result,
                           "duration": durations[1]})
    verification_max_evaluations = len(candidates)
    verification_rows = []
    verification_simulations = 0
    original_verified_quality = current_quality if config["search_grid_size"] == settings.grid_size else None
    original_simulation = current_result if config["search_grid_size"] == settings.grid_size else None
    original_simulation_elapsed = durations[1] if original_simulation is not None else None
    selected = None
    verification = None
    for rank, candidate in enumerate(candidates, 1):
        if cancel.is_set():
            raise InterruptedError("修正板优化已取消")
        publish("verify")
        is_original = candidate["identity"] == original_identity
        feasible = feasible_boards(candidate["masks"])
        failure = None
        simulation_started = time.perf_counter()
        try:
            if config["search_grid_size"] == settings.grid_size:
                result, quality = candidate["result"], candidate["quality"]
                candidate["verified_duration"] = candidate["duration"]
            else:
                verification_simulations += 1
                result = simulate_process(
                    settings, _runtime(recipe, machine, candidate["masks"]),
                    backend=config["backend"], cancel_event=cancel,
                    contribution_source_slot=score_source,
                    fast_cycle=config["fast_cycle"],
                    fast_cycle_residual_percent=config["fast_cycle_residual_percent"])
                quality = _quality(result, config["objective_kind"], config["target_map"])
                candidate["verified_duration"] = time.perf_counter() - simulation_started
        except ValueError as exc:
            result = quality = None
            failure = str(exc)
        verification_evaluations += 1
        if is_original:
            original_verified_quality = quality
            original_simulation = result
            original_simulation_elapsed = candidate.get("verified_duration")
        verification_rows.append({"search_rank": rank, "is_original": is_original,
                                  "feasible": feasible, "search": candidate["quality"],
                                  "verified": quality, "error": failure})
        if quality is not None and feasible and (selected is None or
                quality["score"] < verified_best_quality["score"] - 1e-12 or
                (is_original and abs(quality["score"] - verified_best_quality["score"]) <= 1e-12)):
            selected = candidate
            verification = result
            verified_best_quality = quality
        publish("verify")
    if selected is None:
        raise ValueError("修正板候选在原网格验证中均无有效可行结果")
    best_masks = selected["masks"]
    verified_quality = verified_best_quality
    verification_elapsed = time.perf_counter() - verification_started
    elapsed = time.perf_counter() - started
    report = {
        "mode": config["mode"],
        "initialization": config["initialization"],
        "fixed_mask_slots": list(config["fixed_mask_slots"]),
        "fixed_geometry_unchanged": True,
        "objective": config["objective_kind"],
        "score_scope": score_scope,
        "score_source_slot": score_source,
        "mask_slots": list(slots),
        "search_grid_size": config["search_grid_size"],
        "verification_grid_size": settings.grid_size,
        "algorithm": "fast_cycle" if config["fast_cycle"] else "precise",
        "fast_cycle_residual_percent": config["fast_cycle_residual_percent"],
        "requested_device": config["backend"],
        "actual_device": verification.get("device"),
        "search_evaluations": evaluations,
        "max_evaluations": config["max_evaluations"],
        "elapsed_s": elapsed,
        "verification_elapsed_s": verification_elapsed,
        "selected_simulation_elapsed_s": selected["verified_duration"],
        "original_simulation_elapsed_s": original_simulation_elapsed,
        "original_full_grid_comparison": ("从零设计候选按原网格选优；原板仅用于比较" if not loaded else "原板与入选候选按同一目标在原网格比较"),
        "measured_search_evaluation_s":
            float(np.median(durations)) if durations else None,
        "baseline_without_selected_boards": baseline_quality,
        "original_boards_search": current_quality,
        "original_boards_verified": original_verified_quality,
        "best_search": best_quality,
        "selected_search": selected["quality"],
        "best_verified": verified_quality,
        "search_best_masks": [{"slot": slot, "profile": search_best_masks[slot]} for slot in slots],
        "selected_is_search_best": best_masks == search_best_masks,
        "verification_candidates": verification_rows,
        "verification_evaluations": verification_evaluations,
        "verification_simulations": verification_simulations,
        "verification_shortlist_size": SEARCH_SHORTLIST_SIZE,
        "outline_parameterization": ("loaded_polygon_periodic_radial_deformation" if loaded else "periodic_cubic_radial_bspline"),
        "outline_samples_per_control": OUTLINE_SAMPLES_PER_CONTROL,
        "outline_min_samples_per_control": MIN_OUTLINE_SAMPLES_PER_CONTROL,
        "best_is_original": all(best_masks[slot] == original_masks[slot] for slot in slots),
        "improved_on_search_grid": current_quality is not None and best_quality["score"] < current_quality["score"] - 1e-12,
        "improved_on_verification_grid": original_verified_quality is not None and
            verified_quality["score"] < original_verified_quality["score"] - 1e-12,
        "score_improvement_vs_original_percent":
            ((original_verified_quality["score"] - verified_quality["score"]) /
             original_verified_quality["score"] * 100.0)
            if original_verified_quality is not None and original_verified_quality["score"] > 0 else None,
        "response_scan": response_samples,
        "constraints": {
            "mask_bounds": [
                {"slot": slot,
                 "x_min_mm": config["bounds"][slot].x_min,
                 "x_max_mm": config["bounds"][slot].x_max,
                 "y_min_mm": config["bounds"][slot].y_min,
                 "y_max_mm": config["bounds"][slot].y_max}
                for slot in slots],
            "min_radial_extent_mm": config["min_radial_extent_mm"],
            "min_edge_length_mm": config["min_edge_length_mm"],
            "max_area_fraction": config["max_area_fraction"],
        },
        "warnings": [
            ("已载入无孔多边形以原顶点进行周期径向缩放，不重采样；固定板保留原孔洞。" if loaded else "从零设计无孔径向样条外轮廓，保存为采样多边形；固定板保留原孔洞。"),
            "最小径向尺寸和轮廓边长不等于可制造的最小线宽或孔距。",
            "板面积占包络比例不是沉积效率或通量损失；未做热变形与制造校核。",
            ("搜索前四名与原板按原网格验证后选优；其余候选未复算，不保证全局最优。" if loaded else "搜索前四名按原网格验证后选优；自动设计不以原板兜底，不保证优于原板或全局最优。"),
        ],
    }
    publish("complete")
    return MaskOptimizationResult(
        report=report,
        candidate_masks=[{"slot": slot, "profile": deepcopy(best_masks[slot])}
                         for slot in slots],
        simulation=verification,
        original_simulation=original_simulation,
    )
