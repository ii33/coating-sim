"""Product-model execution paths with optional budgets; no new/general physics."""
from dataclasses import asdict, replace
import math
import time
from pathlib import Path
import numpy as np


class BudgetExceeded(TimeoutError):
    """Cooperative wall-time budget expired (not a completed simulation)."""


class _Stop:
    def __init__(self, cancel, deadline):
        self.cancel, self.deadline = cancel, deadline

    def is_set(self):
        if self.cancel is not None and self.cancel.is_set():
            return True
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise BudgetExceeded("算法时间预算已用尽；当前评估未完成")
        return False


def run_recipe(settings, runtime, *, algorithm="fast", options=None, backend="自动",
               cancel_event=None, fast_cycle=False, fast_cycle_residual_percent=5.,
               progress=None, capture_layer_fields=False, layer_field_spool_path=None):
    from .process_simulator import simulate_process
    if algorithm not in ("fast", "reference", "optimize"):
        raise ValueError("algorithm 必须为 fast、reference 或 optimize")
    options = dict(options or {})
    allowed = {"budget_s", "max_evaluations", "tolerance_percent", "rpm_scale_bounds", "sampling_policy", "periodic_integration"}
    if set(options) - allowed:
        raise ValueError("未知算法选项：" + ", ".join(sorted(set(options) - allowed)))
    sampling_policy = options.get("sampling_policy", "automatic" if algorithm == "reference" else "requested")
    periodic_integration = options.get("periodic_integration", algorithm == "reference")
    if sampling_policy not in ("automatic", "requested") or type(periodic_integration) is not bool:
        raise ValueError("sampling_policy 须为 automatic/requested；periodic_integration 须为布尔值")
    if algorithm != "reference" and (sampling_policy != "requested" or periodic_integration):
        raise ValueError("自动时间采样与有限周期积分仅用于参考路径")
    requested_settings = settings
    sampling_floor = settings.time_steps
    if sampling_policy == "automatic" and settings.carrier_rpm:
        from .process_simulator import process_timing
        profile = runtime["profile"]
        geometry = runtime["machine"]
        timing = process_timing(profile, time_steps=settings.time_steps)
        # Starting density, never an accuracy certificate: both motor phases.
        rpm = abs(settings.carrier_rpm)
        fastest_rpm = rpm * (1 + geometry["sun_teeth"] / geometry["planet_teeth"])
        density = max(79 * rpm / 60., 32 * fastest_rpm / 60.)
        sampling_floor = max(settings.time_steps, math.ceil(timing["total_duration_s"] * density))
        settings = replace(settings, time_steps=sampling_floor)
    # Offline calculations run to their algorithmic stopping condition unless
    # the caller explicitly supplies a wall-time budget (e.g. a benchmark).
    budget = options.get("budget_s")
    tolerance = options.get("tolerance_percent", .1)
    max_evaluations = options.get("max_evaluations", 6 if algorithm == "reference" and sampling_policy == "automatic" else 4 if algorithm == "reference" else 9)
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or not 0 < budget <= 300):
        raise ValueError("budget_s 必须在 0–300 秒之间")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("tolerance_percent 必须为有限正数")
    if type(max_evaluations) is not int or not 1 <= max_evaluations <= 100:
        raise ValueError("max_evaluations 必须为 1–100 整数")
    bounds = options.get("rpm_scale_bounds", [.8, 1.2])
    if not isinstance(bounds, (tuple, list)) or len(bounds) != 2 or any(
            isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in bounds):
        raise ValueError("rpm_scale_bounds 必须为两个有限数")
    lo, hi = bounds
    if not 0 < lo <= 1 <= hi or lo == hi or (algorithm == "optimize" and abs(settings.carrier_rpm) * hi > 300):
        raise ValueError("转速倍率边界须包含初值 1，且保持转速在 ±300 rpm 内")
    recipe_execution_options = {
        "requested": options,
        "effective": {
            "budget_s": budget,
            "max_evaluations": max_evaluations,
            "tolerance_percent": tolerance,
            "rpm_scale_bounds": list(bounds),
            "sampling_policy": sampling_policy,
            "periodic_integration": periodic_integration,
        },
    }
    started = time.monotonic()
    stop = _Stop(cancel_event, started + budget if budget is not None else None)
    trace = []
    capture_spools = []

    def discard_spool(result):
        raw = result.get("layer_field_spool_path") if isinstance(result, dict) else None
        if raw:
            path = Path(raw)
            path.unlink(missing_ok=True)
            capture_spools[:] = [item for item in capture_spools if item != path]

    def evaluate(local, *, cycle=False, device="CPU", scale=1.):
        if stop.is_set():
            raise InterruptedError("计算已取消")
        if progress:
            progress({"algorithm": algorithm, "evaluations": len(trace),
                      "max_evaluations": max_evaluations, "status": "running"})
        capture_path = None
        if capture_layer_fields and layer_field_spool_path is not None:
            base_path = Path(layer_field_spool_path)
            if algorithm == "fast":
                capture_path = base_path
            else:
                capture_path = base_path.with_name(
                    f"{base_path.stem}.evaluation-{len(trace) + 1:04d}{base_path.suffix or '.npz'}")
        try:
            result = simulate_process(local, runtime, backend=device, cancel_event=stop,
                                      fast_cycle=cycle, finite_periodic=periodic_integration,
                                      fast_cycle_residual_percent=fast_cycle_residual_percent,
                                      capture_layer_fields=capture_layer_fields,
                                      layer_field_spool_path=capture_path,
                                       recipe_execution_algorithm=algorithm,
                                       recipe_execution_options=recipe_execution_options,
                                      progress=(lambda value: progress({**value, "algorithm": algorithm,
                                          "evaluations": len(trace), "max_evaluations": max_evaluations,
                                          "status": "running", "fraction": (len(trace) + value["fraction"]) / (1 if algorithm == "fast" else max_evaluations)})) if progress else None)
        except RuntimeError as exc:
            # The legacy backend wraps compute exceptions; preserve a deadline
            # as a budget event so a completed best candidate can still return.
            cause = exc.__cause__
            while cause is not None:
                if isinstance(cause, BudgetExceeded):
                    raise cause
                cause = cause.__cause__
            raise
        if capture_path is not None:
            capture_spools.append(Path(result["layer_field_spool_path"]))
        field = np.asarray(result["field_nm"])
        score = float(np.nanstd(field) / np.nanmean(field) * 100)
        trace.append({"evaluation": len(trace) + 1, "time_steps": local.time_steps,
                      "carrier_rpm": local.carrier_rpm, "rpm_scale": scale,
                      "cv_percent": score})
        return result, score

    status, converged = "completed", None
    details = {}
    if algorithm == "fast":
        result, _ = evaluate(settings, cycle=fast_cycle, device=backend)
        details["scope"] = "工艺积分；使用调用者设备和工艺快算选项"
    elif algorithm == "reference":
        result, _ = evaluate(settings)
        converged, consecutive = False, 0
        status = "evaluation_limit"
        for level in range(1, max_evaluations):
            count = settings.time_steps * 2 ** level
            try:
                refined, _ = evaluate(replace(settings, time_steps=count))
            except BudgetExceeded:
                status = "budget_exhausted"
                break
            previous = result
            old, new = np.asarray(previous["field_nm"]), np.asarray(refined["field_nm"])
            valid = np.isfinite(old) & np.isfinite(new)
            delta = float(np.max(np.abs(new[valid] - old[valid])) / np.mean(new[valid]) * 100)
            trace[-1]["max_field_change_percent"] = delta
            result = refined
            discard_spool(previous)
            consecutive = consecutive + 1 if delta <= tolerance else 0
            if consecutive >= 2:
                converged, status = True, "temporal_converged"
                break
        details.update(scope="CPU float64；有限膜层完整周期+真实相位尾段积分；同网格时间采样加倍" if periodic_integration else "CPU float64；完整工艺时间积分；同网格时间采样加倍",
                       sampling_policy=sampling_policy, initial_time_steps=sampling_floor,
                       requested_time_steps=requested_settings.time_steps,
                       finite_periodic_integration=periodic_integration,
                       starting_density_note="每公转79点且每自转32点为本项目起始约定，不是精度保证；修正板边缘及源离散仍须独立验证",
                       tolerance_percent=tolerance, required_consecutive_passes=2,
                       unverified_dimensions=["空间网格", "有限源离散", "物理模型与生产标定"],
                       warning="时间采样一致性不是严格误差上界；周期混叠仍可能存在，非全局最优或通用精确物理")
    else:
        result, score = evaluate(settings)
        initial_result, initial_score, best_scale = result, score, 1.
        step = (hi - lo) / 2
        search_limit = max(1, max_evaluations - 2)
        status = "evaluation_limit"
        while len(trace) < search_limit:
            improved = False
            center = best_scale
            for candidate in (max(lo, center - step), min(hi, center + step)):
                if len(trace) >= search_limit:
                    break
                if candidate == center:
                    continue
                try:
                    trial, trial_score = evaluate(replace(settings, carrier_rpm=settings.carrier_rpm * candidate), scale=candidate)
                except BudgetExceeded:
                    status = "budget_exhausted"
                    break
                if trial_score < score:
                    previous = result
                    result, score, best_scale, improved = trial, trial_score, candidate, True
                    if previous is not initial_result:
                        discard_spool(previous)
                else:
                    discard_spool(trial)
            if status == "budget_exhausted":
                break
            if not improved:
                step /= 2
            if step < 1e-5 or settings.carrier_rpm == 0:
                status = "local_step_limit"
                break
        provisional_scale, provisional_score = best_scale, score
        provisional_result = result
        result, score, best_scale = initial_result, initial_score, 1.
        if provisional_result is not initial_result:
            discard_spool(provisional_result)
        validation = {"status": "unchanged_initial", "time_steps": None}
        if provisional_scale != 1.:
            count = settings.time_steps * 8
            validation["status"] = "insufficient_evaluations"
            if len(trace) + 2 <= max_evaluations and status != "budget_exhausted":
                validation["time_steps"] = count
                try:
                    baseline, baseline_score = evaluate(replace(settings, time_steps=count))
                    trace[-1]["phase"] = "validation_baseline"
                    discard_spool(result)
                    result, score = baseline, baseline_score
                    candidate, candidate_score = evaluate(replace(settings, time_steps=count,
                        carrier_rpm=settings.carrier_rpm * provisional_scale), scale=provisional_scale)
                    trace[-1]["phase"] = "validation_candidate"
                    validation.update(baseline_cv_percent=baseline_score, candidate_cv_percent=candidate_score)
                    if candidate_score < baseline_score:
                        discard_spool(baseline)
                        result, score, best_scale = candidate, candidate_score, provisional_scale
                        validation["status"] = "accepted_at_finer_sampling"
                    else:
                        discard_spool(candidate)
                        result = baseline
                        score = baseline_score
                        validation["status"] = "rejected_at_finer_sampling"
                except BudgetExceeded:
                    status = "budget_exhausted"
                    validation["status"] = "inconclusive_budget"
            elif status == "budget_exhausted":
                validation["status"] = "inconclusive_budget"
        details.update(scope="现有模型上有界转速倍率坐标搜索；CPU 完整时间积分",
                       objective="等权采样点膜厚变异系数 CV (%)", initial_cv_percent=initial_score,
                       best_cv_percent=score, best_rpm_scale=best_scale,
                       best_carrier_rpm=settings.carrier_rpm * best_scale,
                       accepted_cv_percent=score, accepted_carrier_rpm=settings.carrier_rpm * best_scale,
                       provisional_best_cv_percent=provisional_score,
                       provisional_best_carrier_rpm=settings.carrier_rpm * provisional_scale,
                       validation_status=validation["status"], validation=validation,
                       rpm_scale_bounds=list(bounds),
                       warning="候选须通过8倍时间采样的初值对照才接受；此为同网格排名检查，非收敛认证，空间/源/物理/制造约束未验证")
    report = {"algorithm": algorithm, "status": status, "converged": converged,
              "evaluations": len(trace), "elapsed_s": time.monotonic() - started,
              "budget_s": budget, "trace": trace, **details}
    result["requested_settings"] = asdict(requested_settings)
    result["algorithm_report"] = report
    if layer_field_spool_path is not None and isinstance(result, dict):
        selected = result.get("layer_field_spool_path")
        for path in tuple(capture_spools):
            if str(path) != selected:
                path.unlink(missing_ok=True)
    if progress:
        progress(report)
    return result
