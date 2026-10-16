"""Qt adapter for the independent thin-film thickness optimizer window."""
from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import dataclass, replace
import inspect
import json
from importlib import import_module
import math
from pathlib import Path
import re
import threading
from uuid import uuid4
from typing import Any, cast
import weakref
from coat_sim.project_session import OperationBinding

from PySide6.QtCore import Property, QObject, QRunnable, QUrl, Signal, Slot

from coat_sim.core.film_optimization import (
    FilmConditionError,
    FilmOptimizationObjective,
    FilmOptimizationProgress,
    FilmOptimizationRequest,
    FilmOptimizationResult,
    FilmThicknessVariable,
    freeze_film_optimization_request,
)
from coat_sim.core.film_optimization_preview import create_optimization_preview_handoff
from coat_sim.core.film_optimization_job import CANDIDATE_KIND, RESULT_KIND, deserialize_request
from coat_sim.scheduler import SchedulerClient, freeze_spec
from coat_sim.scheduler.desktop import (
    RemoteCancelEvent,
    observe_job,
    resolve_unknown_submission,
)
from coat_sim.ui.film_optimization_run_coordinator import (
    FilmOptimizationRunCoordinator,
    FilmOptimizationRunPreparationError,
    FilmOptimizationRunStartError,
)


# These values select responsive UI paths only. They never reject an angle
# sample or alter the angle tuple frozen into an optimization request.
_ANGLE_ASYNC_PREPARATION_THRESHOLD = 100_000
_PLOT_ANGLE_OPTION_LIMIT = 101
_ANGLE_TEXT_CONVERSION_LIMIT = 4_096
# QML exposes these integers as JavaScript Numbers; reject values that cannot
# be represented exactly instead of rounding them at the editor boundary.
_MAX_QML_SAFE_INTEGER = (1 << 53) - 1
# The runtime accepts registered Qt type names; the PySide stub exposes only
# Python types here. ``object`` would create a nonnumeric PyObjectWrapper.
_QVARIANT_PROPERTY_TYPE = cast(type, "QVariant")


@dataclass(frozen=True)
class FilmOptimizationSourceSnapshot:
    """Read-only source captured without an editable design session."""

    service: object
    design: dict
    design_revision: int
    path: str
    layers_by_side: dict
    owner_token: object | None = None


def _number(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是有限数字")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须是有限数字") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}必须是有限数字")
    return number


def _objective_default(index: int) -> dict:
    return {
        "id": uuid4().hex,
        "name": f"目标 {index + 1}",
        "wavelength_min_nm": 400.0,
        "wavelength_max_nm": 700.0,
        "wavelength_samples": 31,
        "sampling_basis": "samples",
        "wavelength_step_nm": 10.0,
        "actual_wavelength_step_nm": 10.0,
        "angle_sampling_mode": "range",
        "angle_min_deg": 0.0,
        "angle_max_deg": 0.0,
        "angle_step_deg": 1.0,
        "actual_angle_step_deg": 0.0,
        "angle_sample_count": 1,
        "angles_text": "0",
        "polarization": "unpolarized",
        "metric": "R",
        "mode": "range",
        "target_value_percent": 5.0,
        "lower_bound_percent": 0.0,
        "upper_bound_percent": 5.0,
        "weight": 1.0,
    }


_SOLVER_UI_CHOICES = (
    ("baseline", "基线坐标搜索", "从当前膜厚开始，逐层试探再缩小厚度搜索步幅。适合检查已有方案能否小幅改善；层数多时需要更多评估，不保证全局最优。", "film-thickness-optimization", False),
    ("powell", "Powell 方向集", "不需要光学导数，沿多个厚度组合方向做局部搜索。已有较合理初始膜系、希望联动调整多层时可先用它；需要 SciPy，仍可能停在局部解。", "film-solver-powell", True),
    ("nelder_mead", "Nelder–Mead 单纯形", "用一组厚度候选收缩与移动寻找局部改善，不需要导数。适合较少变量和可接受较多评估的探索；高维单纯形开销随变量数增加，需要 SciPy。", "film-solver-simplex", True),
    ("multistart", "多起点 Powell", "除了当前厚度，还从种子生成的随机起点运行 Powell。怀疑初始方案困在局部解时使用；所有起点共用整次迭代和评估预算，起点越多每个起点分得越少。", "film-solver-multistart", True),
    ("differential_evolution", "差分进化", "让一群厚度候选交叉与扰动，适合初始膜厚不确定、需要较广搜索的情况。群体至少 5；预算最好足以评估多个群体，不能保证找到全局最优。需要 SciPy。", "film-solver-de", True),
    ("simulated_annealing", "模拟退火", "随机扰动厚度，并按逐渐降低的温度偶尔接受较差候选。可用于跳出局部解的有限预算探索；固定随机种子便于复现，不保证全局最优。", "film-solver-sa", False),
    ("trf", "信赖域反射最小二乘（TRF）", "使用解析光学导数降低残差平方和，适合当前单面、正面首表面、受支持光学端口的固定层序膜系。偏重综合残差；最差目标仍单独验收。需要可用的 SciPy 回调版本，材料适用域在运行前校验。", "film-solver-gradient", True),
    ("lbfgsb", "有界拟牛顿（L-BFGS-B）", "使用解析梯度与有限内存更新降低残差平方和。适合受支持的单面固定层序、多变量局部精修；不会改变材料或层数，最差目标仍单独验收，需要 SciPy。", "film-solver-gradient", True),
)
_SOLVER_UI_INDEX = {row[0]: index for index, row in enumerate(_SOLVER_UI_CHOICES)}
_SCIPY_SOLVERS = frozenset(row[0] for row in _SOLVER_UI_CHOICES if row[4])


def _scipy_runtime_availability() -> tuple[str, bool]:
    """Probe the running interpreter without changing its dependency set."""
    try:
        optimize = import_module("scipy.optimize")
    except Exception as exc:
        return f"SciPy 在当前桌面 Python 环境不可导入：{type(exc).__name__}: {exc}", False
    required = ("minimize", "differential_evolution", "least_squares")
    missing = [name for name in required if not callable(getattr(optimize, name, None))]
    if missing:
        return "当前 SciPy 缺少优化接口：" + ", ".join(missing), False
    try:
        trf_callback_available = "callback" in inspect.signature(
            optimize.least_squares).parameters
    except (TypeError, ValueError):
        trf_callback_available = False
    return "", trf_callback_available


def _integer(value: Any, label: str, minimum: int = 1,
             maximum: int | None = _MAX_QML_SAFE_INTEGER) -> int:
    """Accept Qt integral doubles without accepting bool or rounded fractions."""
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是整数")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{label}必须是整数")
        result = int(value)
    else:
        raw = str(value).strip()
        if not re.fullmatch(r"\+?\d+", raw):
            raise ValueError(f"{label}必须是整数")
        result = int(raw)
    if maximum is not None and result > maximum:
        if maximum == _MAX_QML_SAFE_INTEGER:
            raise ValueError(
                f"{label}超过 Qt/QML 可精确表示的整数范围（{_MAX_QML_SAFE_INTEGER}）")
        raise ValueError(f"{label}必须是 {minimum} 到 {maximum} 的整数")
    if result < minimum:
        bounds = f"{minimum} 到 {maximum}" if maximum is not None else f"不小于 {minimum}"
        raise ValueError(f"{label}必须是{bounds}的整数")
    return result


def _resolve_objective_sampling(row: dict, *, edited_field: str | None = None) -> dict:
    """Return one endpoint-inclusive grid, preserving the last sampling basis."""
    result = deepcopy(row)
    low = _number(result["wavelength_min_nm"], "波长下限")
    high = _number(result["wavelength_max_nm"], "波长上限")
    if low <= 0 or high < low:
        raise ValueError("波长必须为正数，且上限不能小于下限（nm）")
    basis = result.get("sampling_basis", "samples")
    if edited_field == "wavelength_step_nm":
        basis = "step"
    elif edited_field == "wavelength_samples":
        basis = "samples"
    if basis not in {"samples", "step"}:
        raise ValueError("未知的波长采样基准")
    count = _integer(result["wavelength_samples"], "波长采样点数", 1)
    width = high - low
    if basis == "step":
        step = _number(result.get("wavelength_step_nm"), "请求采样步长")
        if step <= 0:
            raise ValueError("请求采样步长必须大于 0 nm")
        if width:
            quotient = width / step
            if not math.isfinite(quotient):
                raise ValueError("请求步长无法表示为有限采样网格")
            count = max(2, math.ceil(quotient) + 1)
        else:
            count = 1
    elif width:
        if count == 1:
            if edited_field in {"wavelength_min_nm", "wavelength_max_nm"}:
                count = 2
            else:
                raise ValueError("非零波段至少需要 2 个采样点；单点只适用于相同波长端点")
        step = width / (count - 1)
    else:
        count, step = 1, 0.0
    if count > _MAX_QML_SAFE_INTEGER:
        raise ValueError("波长采样点数超过 Qt/QML 可精确表示的整数范围")
    actual = width / (count - 1) if count > 1 else 0.0
    if count > 1 and actual <= 0:
        raise ValueError("波长采样步长无法表示为正数")
    result.update(wavelength_min_nm=low, wavelength_max_nm=high,
                  wavelength_samples=count, sampling_basis=basis,
                  wavelength_step_nm=step, actual_wavelength_step_nm=actual)
    return result


def _resolve_angle_sampling(row: dict) -> dict:
    """Normalize a range grid or retain a legacy explicit point list."""
    result = deepcopy(row)
    mode = result.get("angle_sampling_mode", "points")
    if mode == "points":
        angles = FilmOptimizationQuickController._parse_angles(
            str(result.get("angles_text", "")))
        result.update(angle_sampling_mode=mode, actual_angle_step_deg=0.0,
                      angle_sample_count=len(angles))
        return result
    if mode != "range":
        raise ValueError("未知的角度采样方式")
    low = _number(result.get("angle_min_deg", 0.0), "入射角下限")
    high = _number(result.get("angle_max_deg", 0.0), "入射角上限")
    step = _number(result.get("angle_step_deg", 1.0), "入射角步长")
    if low < 0 or high < low or high >= 90:
        raise ValueError("入射角范围必须满足 0≤下限≤上限<90°")
    if step <= 0:
        raise ValueError("入射角步长必须大于 0°")
    span = high - low
    if span == 0:
        count, actual = 1, 0.0
    else:
        quotient = span / step
        if not math.isfinite(quotient):
            raise ValueError("入射角步长无法表示为有限采样网格")
        count = max(2, math.ceil(quotient) + 1)
        actual = span / (count - 1)
        if count > _MAX_QML_SAFE_INTEGER:
            raise ValueError("入射角采样点数超过 Qt/QML 可精确表示的整数范围")
        if actual <= 0:
            raise ValueError("入射角实际步长无法表示为正数")
    result.update(angle_sampling_mode=mode, angle_min_deg=low,
                  angle_max_deg=high, angle_step_deg=step,
                  actual_angle_step_deg=actual, angle_sample_count=count)
    return result


def _angle_sample_count(row: dict) -> int:
    """Return a count without materializing a potentially large angle grid."""
    return int(_resolve_angle_sampling(row)["angle_sample_count"])


class _OptimizationSignals(QObject):
    progress = Signal(int, object)
    jobIdChanged = Signal(int, str)
    finished = Signal(int, object, object, object)


@dataclass(frozen=True)
class _PreparedAngleGrid:
    request: Any
    spec: dict


def _angles_for_row_cancellable(row: dict, cancel_event=None, progress=None,
                                objective_index: int = 0,
                                objective_count: int = 1) -> tuple[float, ...]:
    """Materialize one request tuple while permitting responsive cancellation."""
    resolved = _resolve_angle_sampling(row)
    mode = resolved["angle_sampling_mode"]
    if mode == "points":
        source = str(resolved.get("angles_text", ""))
        seen: set[float] = set()

        def point_values():
            count = 0
            for match in re.finditer(r"[^,，;；\s]+", source):
                if count % 4096 == 0 and cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("入射角准备已取消；未提交队列任务。")
                angle = _number(match.group(0), "入射角")
                if angle in seen:
                    raise ValueError("入射角列表不能重复")
                if angle < 0 or angle >= 90:
                    raise ValueError("入射角必须在 0 到小于 90 度之间")
                seen.add(angle)
                count += 1
                if progress is not None and count % 65536 == 0:
                    progress(objective_index, objective_count, count,
                             int(resolved["angle_sample_count"]))
                yield angle

        result = tuple(point_values())
        if not result:
            raise ValueError("请填写至少一个入射角（度），多个角度用逗号分隔")
        return result

    count = int(resolved["angle_sample_count"])
    low = float(resolved["angle_min_deg"])
    high = float(resolved["angle_max_deg"])
    if count == 1:
        return (low,)
    actual = (high - low) / (count - 1)

    def range_values():
        previous = None
        for index in range(count):
            if index % 4096 == 0:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("入射角准备已取消；未提交队列任务。")
                if progress is not None and index % 65536 == 0:
                    progress(objective_index, objective_count, index, count)
            current = high if index == count - 1 else low + index * actual
            if previous is not None and current <= previous:
                raise ValueError("入射角采样网格无法用浮点数严格递增表示")
            previous = current
            yield current

    return tuple(range_values())


class _AngleGridPreparationTask(QRunnable):
    """Prepare large angle tuples off the GUI thread before queue submission."""

    def __init__(self, rows: tuple[dict, ...], request_template,
                 search_mode: str, service, cancel_event: threading.Event,
                 operation_id: int) -> None:
        super().__init__()
        self.rows = rows
        self.request_template = request_template
        self.search_mode = str(search_mode)
        self.service = service
        self.cancel_event = cancel_event
        self.operation_id = int(operation_id)
        self.signals = _OptimizationSignals()

    def _progress(self, objective_index: int, objective_count: int,
                  completed: int, total: int) -> None:
        if total <= 0:
            return
        fraction = (objective_index + completed / total) / max(1, objective_count)
        self.signals.progress.emit(self.operation_id, FilmOptimizationProgress(
            state="validating", fraction=min(0.99, fraction), iteration=0,
            evaluations=0, best_max_normalized_error=None,
            message=f"准备目标 {objective_index + 1}/{objective_count} 的角度网格："
                    f"{completed}/{total} 点"))

    def run(self) -> None:
        try:
            angles = []
            for index, row in enumerate(self.rows):
                if self.cancel_event.is_set():
                    raise InterruptedError("入射角准备已取消；未提交队列任务。")
                angles.append(_angles_for_row_cancellable(
                    row, self.cancel_event, self._progress, index, len(self.rows)))
            if self.cancel_event.is_set():
                raise InterruptedError("入射角准备已取消；未提交队列任务。")
            structure = self.search_mode == "structure"
            base_template = (self.request_template.base_request if structure
                             else self.request_template)
            if len(base_template.objectives) != len(angles):
                raise ValueError("后台准备的角度网格与请求目标数不一致")
            objectives = tuple(replace(item, angles_deg=values)
                               for item, values in zip(base_template.objectives, angles,
                                                       strict=True))
            base_request = replace(base_template, objectives=objectives)
            request = (replace(self.request_template, base_request=base_request)
                       if structure else base_request)
            if self.cancel_event.is_set():
                raise InterruptedError("角度准备已取消；未冻结队列请求。")
            self.signals.progress.emit(self.operation_id, FilmOptimizationProgress(
                state="validating", fraction=0.99, iteration=0, evaluations=0,
                best_max_normalized_error=None,
                message="正在后台冻结材料输入并序列化队列请求"))
            if structure:
                from coat_sim.core.film_optimization_structure import (
                    freeze_film_structure_optimization_request,
                )
                request = freeze_film_structure_optimization_request(self.service, request)
            else:
                request = freeze_film_optimization_request(self.service, request)
            spec = freeze_spec(self.service, "film_optimization", {"request": request})
            if self.cancel_event.is_set():
                raise InterruptedError("角度准备已取消；未提交队列任务。")
            self.signals.finished.emit(
                self.operation_id, _PreparedAngleGrid(request, spec), None, None)
        except InterruptedError as exc:
            self.signals.finished.emit(
                self.operation_id, None, None,
                {"state": "cancelled", "message": str(exc)})
        except Exception as exc:
            self.signals.finished.emit(self.operation_id, None, None, exc)


class _FrozenMaterials:
    """Read-only material rows captured before a worker evaluates candidates."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = {str(row["id"]): deepcopy(row) for row in rows}

    def _records(self, material_ids=None) -> list[dict]:
        if material_ids is None:
            selected = self._rows.values()
        else:
            selected = (self._rows[identifier] for identifier in material_ids
                        if identifier in self._rows)
        return [deepcopy(row) for row in selected]

    def get(self, material_id: str) -> dict:
        if material_id not in self._rows:
            raise ValueError(f"frozen film optimization material missing: {material_id}")
        return deepcopy(self._rows[material_id])


def _film_result_from_queue(value: Any) -> FilmOptimizationResult:
    """Convert the queue's validated JSON result envelope for the existing UI."""
    if (isinstance(value, dict)
            and value.get("kind") == "coat_sim.film_structure_optimization_result"):
        from coat_sim.core.film_optimization_structure import film_structure_result_from_payload

        return film_structure_result_from_payload(value)
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("kind") != RESULT_KIND):
        raise ValueError("queue result is not a typed film optimization result")
    request = deserialize_request(value.get("request", {}))
    candidate = value.get("candidate")
    if (not isinstance(candidate, dict) or candidate.get("kind") != CANDIDATE_KIND
            or candidate.get("schema_version") != 1):
        raise ValueError("queue result is missing its verified candidate")
    if candidate.get("design_revision") != request.design_revision:
        raise ValueError("queue result revision does not match its frozen request")
    quality = candidate.get("quality")
    if not isinstance(quality, dict):
        raise ValueError("queue result is missing candidate quality")
    errors = candidate.get("per_condition_errors")
    if not isinstance(errors, list):
        raise ValueError("queue result condition errors are invalid")
    solver_validation = value.get("solver_validation")
    if solver_validation is None:
        solver_validation = {}
    if not isinstance(solver_validation, dict):
        raise ValueError("queue result solver validation metadata is invalid")
    validation_errors = solver_validation.get("validation_per_condition_errors", [])
    if not isinstance(validation_errors, list):
        raise ValueError("queue result validation condition errors are invalid")
    validation_complete = solver_validation.get("validation_complete", False)
    validation_targets_met = solver_validation.get("validation_targets_met")
    if type(validation_complete) is not bool:
        raise ValueError("queue result validation completion flag is invalid")
    if validation_targets_met is not None and type(validation_targets_met) is not bool:
        raise ValueError("queue result validation target flag is invalid")
    validation_message = str(solver_validation.get("validation_message", ""))
    if not solver_validation and not validation_message:
        validation_message = "此队列结果没有独立留出验证元数据"

    def optional_float(name: str) -> float | None:
        raw = solver_validation.get(name)
        if raw is None:
            return None
        number = float(raw)
        if not math.isfinite(number):
            raise ValueError(f"queue result {name} is not finite")
        return number

    return FilmOptimizationResult(
        state=str(value.get("state", "completed")),
        design_revision=request.design_revision,
        best_design=deepcopy(candidate["design"]),
        best_thickness_nm=deepcopy(candidate["thickness_nm"]),
        max_normalized_error=float(quality["value"]),
        weighted_max_normalized_error=float(quality["weighted_max_normalized_error"]),
        weighted_rms_error=float(quality["weighted_rms_error"]),
        targets_met=bool(quality["targets_met"]),
        per_condition_errors=tuple(FilmConditionError(**row) for row in errors),
        iterations=int(candidate["iterations"]),
        evaluations=int(candidate["evaluations"]),
        message=str(value.get("message", "")),
        solver=str(solver_validation.get("solver", request.solver)),
        seed=int(solver_validation.get("seed", request.seed)),
        termination_reason=str(solver_validation.get("termination_reason", "")),
        failed_evaluations=int(solver_validation.get("failed_evaluations", 0)),
        validation_samples_multiplier=int(solver_validation.get(
            "validation_samples_multiplier", request.validation_samples_multiplier)),
        validation_evaluations=int(solver_validation.get("validation_evaluations", 0)),
        validation_complete=validation_complete,
        validation_max_normalized_error=optional_float("validation_max_normalized_error"),
        validation_weighted_rms_error=optional_float("validation_weighted_rms_error"),
        validation_targets_met=validation_targets_met,
        validation_per_condition_errors=tuple(
            FilmConditionError(**row) for row in validation_errors),
        validation_message=validation_message,
    )


class _FilmOptimizationTask(QRunnable):
    """Compute the baseline from frozen inputs, then observe the shared queue job."""

    def __init__(self, service, request: FilmOptimizationRequest, store,
                 job_id: str, cancel_event: RemoteCancelEvent,
                 operation_id: int, *, remote_admission=None, project_compute=None) -> None:
        super().__init__()
        self.service = service
        self.request = request
        self.store = store
        self.job_id = job_id
        self.cancel_event = cancel_event
        self.operation_id = int(operation_id)
        self.remote_admission = remote_admission
        self.admitted = remote_admission is None
        self._project_compute = project_compute
        self._observer_disconnected = threading.Event()
        self.signals = _OptimizationSignals()
        self.setAutoDelete(False)

    def detach_observer(self) -> None:
        """Stop this client's polling without invoking the cancel transport."""
        self._observer_disconnected.set()

    def _record_state(self, state: str) -> None:
        if self._project_compute is not None:
            self._project_compute.update_job(self.job_id, state, service_owned=self.admitted)

    def _publish_queue_progress(self, value: Any) -> None:
        if isinstance(value, FilmOptimizationProgress):
            self.signals.progress.emit(self.operation_id, value)
            return
        if not isinstance(value, dict):
            return
        state = str(value.get("state", "running"))
        if state not in ("validating", "running", "completed", "cancelled"):
            state = "running"
        error = value.get("best_max_normalized_error", value.get("best_score"))
        try:
            error = float(error) if error is not None else None
        except (TypeError, ValueError, OverflowError):
            error = None
        try:
            progress = FilmOptimizationProgress(
                state=state,
                fraction=max(0.0, min(1.0, float(value.get("fraction", 0.0)))),
                iteration=max(0, int(value.get("iteration", value.get("step_index", 0)))),
                evaluations=max(0, int(value.get("evaluations", 0))),
                best_max_normalized_error=error,
                message=str(value.get("message", value.get("status", ""))),
            )
        except (TypeError, ValueError, OverflowError):
            return
        self.signals.progress.emit(self.operation_id, progress)

    def _verified_terminal_candidate(self) -> FilmOptimizationResult | None:
        """Fetch only a queue-verified checkpoint-backed terminal candidate."""
        if not self.admitted:
            return None

        result_method = getattr(self.store, "result", None)
        if not callable(result_method):
            return None
        try:
            payload = result_method(self.job_id)
            return _film_result_from_queue(payload)
        except Exception:
            # The scheduler rejects this action when cancellation/failure has
            # no replay-validated candidate.  Keep the original terminal error.
            return None

    @Slot()
    def run(self) -> None:
        try:
            if self._observer_disconnected.is_set():
                raise RuntimeError("observer_disconnected: admitted job continues")
            admission_warning = ""
            if self.remote_admission is not None:
                self.signals.progress.emit(self.operation_id, FilmOptimizationProgress(
                    state="validating", fraction=0.0, iteration=0, evaluations=0,
                    best_max_normalized_error=None,
                    message="正在后台核对并提交优化任务",
                ))
                admission_warning = str(self.remote_admission() or "")
                self.job_id = str(self.cancel_event.job_id)
                self.admitted = True
                self._record_state("queued")
                self.signals.jobIdChanged.emit(self.operation_id, self.job_id)
            if self.request.frozen_material_snapshot is None:
                raise ValueError("optimization request was not frozen before submission")
            self.signals.progress.emit(self.operation_id, FilmOptimizationProgress(
                state="validating", fraction=0.0, iteration=0, evaluations=0,
                best_max_normalized_error=None,
                message="正在校验已冻结的材料快照",
            ))
            frozen_service = copy(self.service)
            frozen = self.request.frozen_material_snapshot
            frozen_service.materials = _FrozenMaterials(frozen["materials"])
            frozen_payload = deepcopy(dict(frozen))
            frozen_service.freeze_design = (
                lambda _design, _settings=None, payload=frozen_payload:
                deepcopy(payload)
            )

            # Keep the original-design curve off the GUI thread too. It uses
            # the same submission-time material snapshot as the optimizer.
            baseline = []
            objectives = self.request.objectives
            total = sum(len(item.angles_deg) for item in objectives)
            done = 0
            if admission_warning:
                # A cancellation was requested while local queue admission was
                # racing, but the queue did not accept the cancel. Keep the
                # observer alive for an explicit retry and avoid extra local
                # simulation work while cancellation is pending.
                self.signals.progress.emit(self.operation_id, FilmOptimizationProgress(
                    state="running", fraction=0.0, iteration=0, evaluations=0,
                    best_max_normalized_error=None, message=admission_warning))
            for objective_index, objective in (() if admission_warning else enumerate(objectives)):
                settings = {
                    "wavelength_min_nm": objective.wavelength_min_nm,
                    "wavelength_max_nm": objective.wavelength_max_nm,
                    "samples": objective.wavelength_samples,
                    "polarization": objective.polarization,
                    "first_surface": self.request.design_snapshot.get(
                        "first_surface", "front"),
                }
                for angle in objective.angles_deg:
                    if self.cancel_event.is_set() or self._observer_disconnected.is_set():
                        break
                    response = frozen_service.simulate(
                        self.request.design_snapshot,
                        {**settings, "angle_deg": angle},
                    )
                    baseline.append({
                        "objective_index": objective_index,
                        "angle_deg": angle,
                        "wavelength_nm": response["wavelength_nm"],
                        "actual": response[objective.metric],
                    })
                    done += 1
                    self.signals.progress.emit(self.operation_id, FilmOptimizationProgress(
                        state="running",
                        fraction=0.04 * done / max(1, total),
                        iteration=0,
                        evaluations=0,
                        best_max_normalized_error=None,
                        message="正在计算原设计光谱",
                    ))

            payload = observe_job(
                self.store, self.job_id, self._publish_queue_progress,
                disconnected=self._observer_disconnected.is_set,
            )
            result = _film_result_from_queue(payload)
            self._record_state("succeeded")
            self.signals.finished.emit(self.operation_id, result, baseline, None)
        except InterruptedError as exc:
            self._record_state("cancelled")
            result = self._verified_terminal_candidate()
            if self.remote_admission is not None and not self.admitted:
                self.signals.jobIdChanged.emit(self.operation_id, "")
            self.signals.finished.emit(
                self.operation_id, result, baseline if "baseline" in locals() else [],
                {"state": "cancelled", "message": str(exc)},
            )
        except Exception as exc:
            if self._observer_disconnected.is_set():
                self._record_state("detached")
                self.signals.finished.emit(self.operation_id, None, [],
                                           {"state": "detached", "message": str(exc)})
                return
            self._record_state("failed")
            from coat_sim.scheduler.remote_client import RemoteSubmissionUnknown
            if (not self.admitted and
                    isinstance(exc, RemoteSubmissionUnknown)):
                # A lost acknowledgement is not a rejection. Preserve the
                # original (or recovered same-input) ID and never repost.
                self.job_id = str(exc.job_id)
                self.signals.jobIdChanged.emit(self.operation_id, self.job_id)
                self.signals.finished.emit(
                    self.operation_id, None,
                    baseline if "baseline" in locals() else [],
                    {
                        "state": "submission_unknown",
                        "job_id": self.job_id,
                        "message": (
                            "远程提交状态仍未知；不会自动重复提交。"
                            f"请使用作业 ID {self.job_id} 核对状态。"
                        ),
                    },
                )
                return
            if self.remote_admission is not None and not self.admitted:
                # Capabilities failures and explicit admission rejection are
                # known pre-admission failures; they must not query result().
                self.signals.finished.emit(
                    self.operation_id, None,
                    baseline if "baseline" in locals() else [],
                    {"state": "submission_failed", "message": str(exc)},
                )
                return
            result = self._verified_terminal_candidate()
            self.signals.finished.emit(
                self.operation_id, result, baseline if "baseline" in locals() else [],
                f"{type(exc).__name__}: {exc}")


class _LocalAdmissionCancelEvent(RemoteCancelEvent):
    """Remember local cancellation intent while an async submit has no row yet.

    ``RemoteCancelEvent`` intentionally calls local cancellation synchronously,
    which is right for an already-admitted job but can lose a cancellation if
    the queue row has not been created yet. This token defers that attempt
    until the exact asynchronous submit returns successfully, and retains the
    normal retry behavior if an admitted cancel fails.
    """

    def __init__(self, client, job_id: str) -> None:
        super().__init__(client, job_id)
        self.cancel_intent = threading.Event()
        self._admission_lock = threading.Lock()
        self._queue_admitted = False
        self.cancel_error_message = ""

    def set(self) -> None:
        self.cancel_intent.set()
        with self._admission_lock:
            admitted = self._queue_admitted
        if not admitted or threading.Event.is_set(self):
            return
        try:
            self.client.cancel(self.job_id)
        except Exception as exc:
            self.cancel_error_message = f"{type(exc).__name__}: {exc}"
            raise
        self.cancel_error_message = ""
        threading.Event.set(self)

    def mark_admitted(self) -> None:
        with self._admission_lock:
            self._queue_admitted = True

class _QueueCompatibilityError(RuntimeError):
    pass


class FilmOptimizationQuickController(QObject):
    """Per-window optimization state; the edit session remains the design owner."""

    changed = Signal()
    plotDataChanged = Signal()
    overwriteRequested = Signal(str)
    closeConfirmed = Signal()
    closeConfirmationRequested = Signal()
    applyConfirmationRequested = Signal(str)
    sourceReuseConfirmationRequested = Signal()

    _sessionChange = Signal(object)

    # Private forwarding properties retain the controller's existing internal
    # test seams while keeping all attempt state in one composition owner.
    @property
    def _pool(self):
        return self._run_coordinator._pool

    @property
    def _worker(self):
        return self._run_coordinator.worker

    @_worker.setter
    def _worker(self, value):
        self._run_coordinator._worker = value

    @property
    def _cancel_event(self):
        return self._run_coordinator.cancel_event

    @_cancel_event.setter
    def _cancel_event(self, value):
        self._run_coordinator._cancel_event = value

    @property
    def _busy(self):
        return self._run_coordinator.busy

    @_busy.setter
    def _busy(self, value):
        self._run_coordinator._busy = bool(value)

    @property
    def _operation_serial(self):
        return self._run_coordinator.operation_serial

    @_operation_serial.setter
    def _operation_serial(self, value):
        self._run_coordinator._operation_serial = int(value)

    @property
    def _active_operation_id(self):
        return self._run_coordinator.active_operation_id

    @_active_operation_id.setter
    def _active_operation_id(self, value):
        self._run_coordinator._active_operation_id = value

    @property
    def _operation_edit_epoch(self):
        return self._run_coordinator.operation_edit_epoch

    @_operation_edit_epoch.setter
    def _operation_edit_epoch(self, value):
        self._run_coordinator._operation_edit_epoch = value

    @property
    def _cancel_requested(self):
        return self._run_coordinator.cancel_requested

    @_cancel_requested.setter
    def _cancel_requested(self, value):
        self._run_coordinator._cancel_requested = bool(value)

    @property
    def _pending_close(self):
        return self._run_coordinator.pending_close

    @_pending_close.setter
    def _pending_close(self, value):
        self._run_coordinator._pending_close = bool(value)

    @property
    def _shutting_down(self):
        return self._run_coordinator.shutting_down

    @_shutting_down.setter
    def _shutting_down(self, value):
        self._run_coordinator._shutting_down = bool(value)

    def __init__(self, source_controller, parent: QObject | None = None, *,
                 project_context=None) -> None:
        super().__init__(parent)
        project_context = (project_context if project_context is not None
                           else getattr(source_controller, "project_context", None))
        self._run_coordinator = FilmOptimizationRunCoordinator(
            self, service_owned=project_context is not None)
        self._run_coordinator.progress.connect(self._on_progress_for_operation)
        self._run_coordinator.jobIdChanged.connect(self._on_job_id_for_operation)
        self._run_coordinator.finished.connect(self._on_finished)
        self._snapshot_source = (
            source_controller
            if isinstance(source_controller, FilmOptimizationSourceSnapshot) else None)
        self._snapshot_owner_token = getattr(self._snapshot_source, "owner_token", None)
        self._source_controller = (None if self._snapshot_source is not None
                                   else source_controller)
        self.session = (None if self._snapshot_source is not None
                        else getattr(source_controller, "session", None))
        self.service = (self._snapshot_source.service if self._snapshot_source is not None
                        else getattr(source_controller, "service", None))
        self._detached_source = self._snapshot_source is not None
        self.project_context = (project_context if project_context is not None
                                else getattr(source_controller, "project_context", None))
        self._project_compute = None
        self._project_operation: OperationBinding | None = None
        self._pending_project_completion: tuple[Any, ...] | None = None
        self._pending_angle_preparation: dict | None = None
        self._prepared_angle_job_id: str | None = None
        self._admission_stopped = threading.Event()
        if self.project_context is not None:
            if self.service is not self.project_context.service:
                raise ValueError("Project optimizer requires its project optical service")
            if self.session is not None and self.session is not self.project_context.editor:
                raise ValueError("Project optimizer requires its project editor")
            from coat_sim.core.project_compute import ProjectComputeWorkspace
            self._project_compute = ProjectComputeWorkspace.for_project(
                self.project_context.host, self.project_context.binding)
            self._project_compute.register_thaw_callback(self._on_project_unfrozen)
        if self.service is None or (self.session is None and not self._detached_source):
            raise ValueError("film optimization requires a design source and film service")

        from coat_sim.ui.compute_target import ComputeTarget
        # Some read-only/in-memory design previews have no service root. The
        # established submit path still requires one; construction should not
        # fail merely because a preview never starts an optimization.
        selection_root = getattr(self.service, "root", None) or getattr(source_controller, "root", None) or Path.cwd()
        self._compute_target = ComputeTarget(selection_root, "film_optimization", self,
                                             client_factory=SchedulerClient)

        self._configuration_loading = True
        self._configuration_load_error = ""
        self._configuration_restore_notice = ""
        self._pending_reuse_source = None
        self._pending_reuse_start = False
        self._source_closed = False
        self._observers_detached = False
        self._session_unsubscribe = None
        self._source_connections = []
        self._progress = 0.0
        self._progress_iteration = 0
        self._progress_evaluations = 0
        self._progress_best_error: float | None = None
        self._progress_text = "尚未开始"
        self._error = ""
        self._result: FilmOptimizationResult | None = None
        self._result_terminal_state = ""
        self._result_application_eligible = False
        self._frozen_request: FilmOptimizationRequest | None = None
        self._queue_job_id = ""
        self._completion_handoff_done = False
        self._baseline: dict[tuple[int, float], dict] = {}
        self._candidate_saved = False
        self._candidate_applied = False
        self._search_mode = "fixed_topology"
        self._structure_mode = "combined"
        self._structure_material_options: list[dict] = []
        self._structure_material_error = ""
        self._structure_options = {
            "max_total_thickness_nm": 2000.0,
            "min_thickness_nm": 0.5,
            "max_thickness_nm": 200.0,
            "max_total_layers": 32,
            "max_added_layers": 1,
            "position_samples": 17,
            "candidate_budget": 128,
            "needle_thickness_samples": 9,
            "growth_steps_nm": (0.5, 1.0, 2.0, 5.0, 10.0),
            "growth_phase_fractions": (0.25, 0.5, 1.0),
            "growth_cycles": 1,
            "seed": 0,
        }
        self._structure_result: Any = None
        self._acceptable_error_percent = 0.1
        self._solver_id = "baseline"
        self._max_iterations = 200
        self._max_evaluations = 1000
        self._seed = 0
        self._validation_samples_multiplier = 2
        self._multistart_count = 4
        self._population_size = 12
        self._pending_edit_error = False
        self._pending_edit_error_key: str | None = None
        self._scipy_import_error, self._trf_callback_available = (
            _scipy_runtime_availability())
        self._objectives = [_objective_default(0)]
        self._inspection_objectives: list[dict] | None = None
        self._selected_objective = 0
        self._plot_objective = 0
        self._plot_angle = 0.0
        self._source_revision = -1
        self._source_stale = False
        self._source_document_replaced = False
        self._design = {}
        self._path = ""
        self._variables: list[dict] = []
        self._load_source(preserve_variables=False)
        self._refresh_structure_material_options(preserve_selection=False)
        self._configuration_defaults = self.exportOptimizationConfiguration()
        self._configuration_loading = False
        self._restore_session_configuration(reset_missing=False)

        self._attach_source_observers()

    @Property(bool, constant=True)
    def projectMode(self) -> bool:
        return self.project_context is not None

    @Property(bool, notify=changed)
    def localWorkRunning(self) -> bool:
        worker = self._worker
        return bool(self._busy and (
            self._pending_angle_preparation is not None
            or (worker is not None
                and getattr(worker, "remote_admission", None) is not None
                and not getattr(worker, "admitted", False))))

    @Slot()
    def cancelLocalWork(self) -> None:
        if self.localWorkRunning:
            self.cancelOptimization()

    def _on_project_unfrozen(self) -> None:
        pending, self._pending_project_completion = self._pending_project_completion, None
        if pending is not None and not self._shutting_down:
            self._on_finished(*pending)

    def _project_current(self) -> bool:
        return (self._project_compute is None or
                (self._project_operation is not None
                 and self._project_compute.accepts(self._project_operation)))

    @Property(QObject, constant=True)
    def computeTarget(self):
        return self._compute_target

    def _attach_source_observers(self) -> None:
        source_controller = self._source_controller
        if self.session is not None:
            self._sessionChange.connect(self._on_session_change)
            controller_ref = weakref.ref(self)

            def notify(change) -> None:
                controller = controller_ref()
                if controller is not None and not controller._observers_detached:
                    try:
                        controller._sessionChange.emit(change)
                    except RuntimeError:
                        # A session can finish notifying while its Qt owner is
                        # being torn down.  The subscription is detached below;
                        # this guard covers an already-copied callback list.
                        return

            self._session_unsubscribe = self.session.subscribe(notify)
            pending_edit_changed = getattr(source_controller, "pendingEditEpochChanged", None)
            if pending_edit_changed is not None and hasattr(pending_edit_changed, "connect"):
                pending_edit_changed.connect(self._on_source_pending_edit)
                self._source_connections.append(
                    (pending_edit_changed, self._on_source_pending_edit))
            close_confirmed = getattr(source_controller, "closeConfirmed", None)
            if close_confirmed is not None and hasattr(close_confirmed, "connect"):
                close_confirmed.connect(self.markSourceClosed)
                self._source_connections.append((close_confirmed, self.markSourceClosed))
            source_destroyed = getattr(source_controller, "destroyed", None)
            if source_destroyed is not None and hasattr(source_destroyed, "connect"):
                source_destroyed.connect(self.markSourceClosed)
                self._source_connections.append((source_destroyed, self.markSourceClosed))

    @Slot(result="QVariantMap")
    def exportOptimizationConfiguration(self) -> dict:
        """Return JSON-only editor state; candidates and active jobs are excluded."""
        return {
            "version": 1,
            "objectives": deepcopy(self._objectives),
            "variables": [{key: deepcopy(row[key]) for key in
                           ("side", "layer_id", "enabled", "lower_nm", "upper_nm")}
                          for row in self._variables],
            "solver": {
                "method": self._solver_id,
                "acceptable_error_percent": self._acceptable_error_percent,
                "max_iterations": self._max_iterations,
                "max_evaluations": self._max_evaluations,
                "seed": self._seed,
                "validation_samples_multiplier": self._validation_samples_multiplier,
                "multistart_count": self._multistart_count,
                "population_size": self._population_size,
            },
            "search_mode": self._search_mode,
            "structure_mode": self._structure_mode,
            "structure_options": {key: list(value) if isinstance(value, tuple) else value
                                  for key, value in self._structure_options.items()},
            "structure_materials": [{"material_id": row["material_id"],
                                     "optical_set_id": row["optical_set_id"]}
                                    for row in self._structure_material_options if row.get("selected")],
        }

    @Slot("QVariantMap", result=bool)
    def loadOptimizationConfiguration(self, configuration: dict) -> bool:
        """Strict public import; never guess the identity of a saved variable."""
        return self._load_optimization_configuration(configuration, reconcile_layers=False)

    def _load_optimization_configuration(self, configuration: dict, *,
                                         reconcile_layers: bool) -> bool:
        """Restore atomically; document reloads may reconcile orphaned references."""
        if self._busy or self._shutting_down:
            return False
        try:
            if not isinstance(configuration, dict):
                raise ValueError("优化配置必须为对象")
            json.dumps(configuration, allow_nan=False)
            allowed = {"version", "objectives", "variables", "solver", "search_mode",
                       "structure_mode", "structure_options", "structure_materials"}
            if (set(configuration) - allowed or type(configuration.get("version")) is not int
                    or configuration.get("version") != 1):
                raise ValueError("优化配置版本或字段不受支持")
            defaults = deepcopy(self._configuration_defaults)
            raw_objectives = configuration.get("objectives", defaults["objectives"])
            if not isinstance(raw_objectives, list):
                raise ValueError("优化目标必须是数组")
            objectives, ids = [], set()
            target_fields = set(_objective_default(0))
            for index, raw in enumerate(raw_objectives):
                if not isinstance(raw, dict) or set(raw) - target_fields:
                    raise ValueError(f"目标 {index + 1} 字段无效")
                row = {**_objective_default(index), **deepcopy(raw)}
                # Configuration schema v1 originally stored a free-form angle
                # list. Preserve it exactly, including nonuniform legacy lists.
                if "angle_sampling_mode" not in raw:
                    row["angle_sampling_mode"] = "points" if "angles_text" in raw else "range"
                if not isinstance(row["id"], str) or not row["id"] or row["id"] in ids:
                    raise ValueError("目标 ID 必须为非空且互不重复的字符串")
                ids.add(row["id"])
                row = _resolve_objective_sampling(row)
                row = _resolve_angle_sampling(row)
                if (row["polarization"] not in {"s", "p", "unpolarized"}
                        or row["metric"] not in {"R", "T"}
                        or row["mode"] not in {"value", "range"}):
                    raise ValueError(f"目标 {index + 1} 偏振、指标或类型无效")
                for name in ("target_value_percent", "lower_bound_percent", "upper_bound_percent"):
                    row[name] = _number(row[name], name)
                    if not 0 <= row[name] <= 100:
                        raise ValueError(f"目标 {index + 1} R/T 百分比须在 0 到 100 之间")
                if row["lower_bound_percent"] > row["upper_bound_percent"]:
                    raise ValueError(f"目标 {index + 1} 下界不能大于上界")
                row["weight"] = _number(row["weight"], "目标权重")
                if row["weight"] <= 0:
                    raise ValueError("目标权重必须大于 0")
                if not isinstance(row["name"], str) or not isinstance(row["angles_text"], str):
                    raise ValueError("目标名称和角度输入必须为文本")
                objectives.append(row)
            variables = deepcopy(self._variables)
            by_id = {(row["side"], row["layer_id"]): row for row in variables}
            if len(by_id) != len(variables):
                raise ValueError("当前设计的膜层引用重复，无法恢复约束")
            raw_variables = configuration.get("variables", [])
            if not isinstance(raw_variables, list):
                raise ValueError("优化变量必须是数组")
            # A document's saved scope is authoritative. New layers are never
            # opted into optimization just because their old neighbour vanished.
            if reconcile_layers and "variables" in configuration:
                for row in variables:
                    row["enabled"] = False
            seen = set()
            orphaned = []
            for raw in raw_variables:
                if (not isinstance(raw, dict)
                        or set(raw) != {"side", "layer_id", "enabled", "lower_nm", "upper_nm"}):
                    raise ValueError("优化变量字段无效")
                if (not isinstance(raw["side"], str) or raw["side"] not in {"front", "back"}
                        or not isinstance(raw["layer_id"], str) or not raw["layer_id"]):
                    raise ValueError("优化变量的膜面或膜层 ID 无效")
                key = (raw["side"], raw["layer_id"])
                if key in seen:
                    raise ValueError(f"优化变量对应膜层重复：{key}")
                seen.add(key)
                if type(raw["enabled"]) is not bool:
                    raise ValueError("变量启用状态必须是布尔值")
                lower = _number(raw["lower_nm"], "厚度下界")
                upper = _number(raw["upper_nm"], "厚度上界")
                if raw["enabled"] and (lower <= 0 or upper <= lower):
                    raise ValueError(f"所选膜层 {raw['layer_id']} 的厚度边界必须为正且递增")
                if key not in by_id:
                    if not reconcile_layers:
                        raise ValueError(f"优化变量对应膜层不存在：{key}")
                    orphaned.append(key)
                    continue
                by_id[key].update(enabled=raw["enabled"], lower_nm=lower, upper_nm=upper)
            new_layers = set(by_id) - seen if "variables" in configuration else set()
            restore_notice = ""
            if orphaned or (reconcile_layers and new_layers):
                parts = ["已恢复目标、算法和仍匹配的膜层约束。"]
                if orphaned:
                    references = "、".join(f"{side}/{layer_id}" for side, layer_id in orphaned)
                    parts.append(f"{len(orphaned)} 个旧膜层约束已失效，未转移到其它膜层：{references}。")
                if new_layers:
                    parts.append(f"恢复时未自动勾选 {len(new_layers)} 个新膜层；请在约束页核对选择与厚度范围。")
                restore_notice = "".join(parts)
            solver = {**defaults["solver"], **configuration.get("solver", {})}
            if set(solver) != set(defaults["solver"]) or solver["method"] not in _SOLVER_UI_INDEX:
                raise ValueError("优化方法或参数字段无效")
            for name in ("max_iterations", "max_evaluations", "validation_samples_multiplier",
                         "multistart_count", "population_size"):
                solver[name] = _integer(solver[name], name, 1)
            solver["seed"] = _integer(solver["seed"], "seed", 0, 0xFFFFFFFF)
            acceptable = _number(solver["acceptable_error_percent"], "目标误差容限")
            if not 0 <= acceptable <= 100:
                raise ValueError("目标误差容限须在 0 到 100 个百分点之间")
            if solver["method"] == "differential_evolution" and solver["population_size"] < 5:
                raise ValueError("差分进化群体数至少为 5")
            search_mode = configuration.get("search_mode", "fixed_topology")
            structure_mode = configuration.get("structure_mode", "combined")
            if search_mode not in {"fixed_topology", "structure"} or structure_mode not in {"combined", "needle_only", "growth_only"}:
                raise ValueError("优化或结构模式无效")
            structure = {**defaults["structure_options"], **configuration.get("structure_options", {})}
            if set(structure) != set(defaults["structure_options"]):
                raise ValueError("结构搜索配置字段无效")
            for name in ("max_total_layers", "max_added_layers", "position_samples",
                         "candidate_budget", "needle_thickness_samples", "growth_cycles"):
                # Layer count and one-pass algorithm controls retain their
                # domain limits below; compute budgets/sampling counts do not.
                structure[name] = _integer(structure[name], name, 1)
            structure["seed"] = _integer(structure["seed"], "seed", 0, 0xFFFFFFFF)
            if structure["max_total_layers"] > 4096 or structure["max_added_layers"] != 1 \
                    or structure["growth_cycles"] != 1:
                raise ValueError("结构层数或搜索轮次超出当前算法范围")
            for name in ("max_total_thickness_nm", "min_thickness_nm", "max_thickness_nm"):
                structure[name] = _number(structure[name], name)
                if structure[name] <= 0:
                    raise ValueError("结构厚度参数必须大于 0 nm")
            if structure["max_thickness_nm"] <= structure["min_thickness_nm"]:
                raise ValueError("结构单层厚度上界必须大于下界")
            for name in ("growth_steps_nm", "growth_phase_fractions"):
                if not isinstance(structure[name], (list, tuple)):
                    raise ValueError("结构增长序列必须为数组")
                structure[name] = self._parse_positive_float_list(
                    ",".join(str(value) for value in structure[name]), name,
                    maximum=1.0e6, allow_fraction=name == "growth_phase_fractions")
            refs = configuration.get("structure_materials", [])
            if not isinstance(refs, list):
                raise ValueError("结构候选材料必须为数组")
            selected_refs = set()
            available_refs = {(row["material_id"], row["optical_set_id"])
                              for row in self._structure_material_options}
            for raw in refs:
                if not isinstance(raw, dict) or set(raw) != {"material_id", "optical_set_id"}:
                    raise ValueError("结构候选材料字段无效")
                key = (raw["material_id"], raw["optical_set_id"])
                if key not in available_refs or key in selected_refs:
                    raise ValueError(f"结构候选材料已不可用或重复：{key}")
                selected_refs.add(key)
        except (TypeError, ValueError, OverflowError) as exc:
            self._configuration_load_error = f"未恢复优化配置：{exc}"
            self._error = self._configuration_load_error
            self.changed.emit()
            return False
        self._configuration_loading = True
        try:
            self._objectives, self._variables = objectives, variables
            self._selected_objective = 0 if objectives else -1
            self._plot_objective, self._plot_angle = 0, 0.0
            self._solver_id, self._acceptable_error_percent = solver["method"], acceptable
            for name in ("max_iterations", "max_evaluations", "seed", "validation_samples_multiplier",
                         "multistart_count", "population_size"):
                setattr(self, "_" + name, solver[name])
            self._search_mode, self._structure_mode = search_mode, structure_mode
            self._structure_options = structure
            for row in self._structure_material_options:
                row["selected"] = (row["material_id"], row["optical_set_id"]) in selected_refs
            self._pending_edit_error = False
            self._pending_edit_error_key = None
            self._configuration_load_error = self._error = ""
            self._configuration_restore_notice = restore_notice
            self._invalidate_result()
        finally:
            self._configuration_loading = False
        return True

    def _persist_configuration(self) -> None:
        if self._configuration_loading or self.session is None:
            return
        setter = getattr(self.session, "set_workflow_configuration", None)
        getter = getattr(self.session, "workflow_configuration", None)
        if not callable(setter):
            return
        configuration = self.exportOptimizationConfiguration()
        try:
            if callable(getter) and getter("optimization") == configuration:
                return
            if not setter("optimization", configuration):
                raise ValueError("文档会话拒绝优化配置")
        except (TypeError, ValueError, RuntimeError) as exc:
            self._configuration_load_error = f"优化配置未写入文档：{exc}"
            self._error = self._configuration_load_error
            self.changed.emit()

    def _restore_session_configuration(self, *, reset_missing: bool) -> bool:
        getter = getattr(self.session, "workflow_configuration", None)
        configuration = (getter("optimization") if callable(getter)
                         else deepcopy(self._design.get("workflow_settings", {}).get("optimization", {})))
        if configuration:
            if configuration == self.exportOptimizationConfiguration():
                previous_error = self._configuration_load_error
                self._configuration_load_error = ""
                if previous_error and self._error == previous_error:
                    self._error = ""
                if previous_error:
                    self.changed.emit()
                return True
            return self._load_optimization_configuration(configuration, reconcile_layers=True)
        if reset_missing:
            self._configuration_restore_notice = ""
            defaults = deepcopy(self._configuration_defaults)
            defaults["variables"] = [{key: row[key] for key in
                                     ("side", "layer_id", "enabled", "lower_nm", "upper_nm")}
                                    for row in self._variables]
            defaults["structure_materials"] = [{"material_id": row["material_id"],
                                               "optical_set_id": row["optical_set_id"]}
                                              for row in self._structure_material_options if row.get("selected")]
            return self.loadOptimizationConfiguration(defaults)
        return True

    @Property(str, notify=changed)
    def configurationRestoreNotice(self) -> str:
        return self._configuration_restore_notice

    @Property(str, notify=changed)
    def samplingSummary(self) -> str:
        if not 0 <= self._selected_objective < len(self._objectives):
            return ""
        row = self._objectives[self._selected_objective]
        if row["wavelength_min_nm"] == row["wavelength_max_nm"]:
            return "单波长：只计算 1 点，实际步长 0 nm。"
        actual = row["actual_wavelength_step_nm"]
        return (f"包含波段两端，共 {row['wavelength_samples']} 点；实际步长 {actual:.9g} nm。"
                + (f"请求不超过 {row['wavelength_step_nm']:.9g} nm；不整除时均匀缩短。"
                   if row["sampling_basis"] == "step" else "编辑波段时保留点数。"))

    @Property(str, notify=changed)
    def angleSamplingSummary(self) -> str:
        if not 0 <= self._selected_objective < len(self._objectives):
            return ""
        row = self._objectives[self._selected_objective]
        try:
            resolved = _resolve_angle_sampling(row)
        except (TypeError, ValueError, OverflowError) as exc:
            return str(exc)
        if resolved["angle_sample_count"] == 1:
            return "单角度：只计算 1 个入射角样本。"
        if resolved["angle_sampling_mode"] == "points":
            return f"显式角度列，共 {resolved['angle_sample_count']} 个样本；保留各点原值。"
        return (f"包含角度范围两端，共 {resolved['angle_sample_count']} 个角度；"
                f"实际步长 {resolved['actual_angle_step_deg']:.9g}°。"
                "不整除时均匀缩短步长。")

    @Property(str, notify=changed)
    def resourceRiskSummary(self) -> str:
        selected = sum(bool(row["enabled"]) for row in self._variables)
        layers = len(self._variables)
        conditions = 0
        try:
            conditions = sum(row["wavelength_samples"] * _angle_sample_count(row)
                             for row in self._objectives)
        except ValueError:
            pass
        return (f"膜层 {layers} 层，选中 {selected} 个厚度变量，训练条件 {conditions} 个。"
                "变量、采样和群体越多，耗时与内存越高；请按资源调整显式预算，必要时取消。"
                "文档层数遵守 4096 层解析边界。")

    @Property(str, notify=changed)
    def solverParameterGuide(self) -> str:
        return ("最大评估数是整次搜索真正计算完整光学候选的总预算，例如先用 100 次试跑，再提高到 1000 次；"
                "最大迭代数控制搜索轮次，不等于评估次数。随机种子相同有助于复现随机方法。"
                "验证倍率 2 在相邻训练波长之间加一点，检查遗漏窄峰；倍率 1 不增加波长点，不可当作独立留出。"
                "多起点数 4 表示最多从四处启动 Powell，共享预算。差分进化群体数 12 是候选总数，不是每层 12 个；"
                "群体越大每轮花费越多。加密检查仍不能保证连续光谱精度或全局最优。")

    @Slot(str, "QVariant", result=bool)
    def reuseWindowOpen(self, document_path: str = "", payload=None) -> bool:
        """Safely bind a singleton optimizer to an explicitly requested source."""
        if payload is None:
            if document_path:
                self._error = "请从设计或仿真窗口打开膜系优化，以确定来源会话。"
                self.changed.emit()
                return False
            return True
        if self._shutting_down:
            return False
        valid = (getattr(payload, "service", None) is not None
                 and (isinstance(payload, FilmOptimizationSourceSnapshot)
                      or getattr(payload, "session", None) is not None))
        if not valid:
            self._error = "优化来源无效；当前来源保持不变。"
            self.changed.emit()
            return False
        same_session = (self.session is not None
                        and getattr(payload, "session", None) is self.session)
        same_snapshot = (isinstance(payload, FilmOptimizationSourceSnapshot)
                         and self._detached_source and payload.owner_token is not None
                         and payload.owner_token is self._snapshot_owner_token)
        if same_session and not self._source_closed and payload is self._source_controller:
            keep_candidate = (self._result is not None
                              and not self._candidate_saved and not self._candidate_applied)
            if (self._source_stale and not self._busy and not self._pending_edit_error
                    and not keep_candidate):
                self.refreshDesign()
            return True
        if same_snapshot:
            return payload is self._snapshot_source or self.refreshDetachedSource(payload)
        if self._busy:
            self._error = "优化正在计算；先取消并等待当前任务结束，再切换来源。"
            self.changed.emit()
            return False
        if self._pending_edit_error:
            self._error = "请先修正尚未提交的优化输入，再切换来源。"
            self.changed.emit()
            return False
        if self._result is not None and not self._candidate_saved and not self._candidate_applied:
            self._pending_reuse_source = payload
            self._pending_reuse_start = False
            self.sourceReuseConfirmationRequested.emit()
            return True
        return self._replace_source(payload)

    @Slot(bool)
    def resolveSourceReuse(self, discard_candidate: bool) -> None:
        source = self._pending_reuse_source
        start = self._pending_reuse_start
        self._pending_reuse_source = None
        self._pending_reuse_start = False
        if discard_candidate and source is not None and self._replace_source(source) and start:
            self.startOptimizationFromShortcut()

    def _replace_source(self, source) -> bool:
        if self._busy or self._shutting_down:
            return False
        if self.project_context is not None and (
                getattr(source, "service", None) is not self.project_context.service
                or (not isinstance(source, FilmOptimizationSourceSnapshot)
                    and getattr(source, "session", None) is not self.project_context.editor)):
            self._error = "不能把其他项目的来源绑定到此优化窗口。"
            self.changed.emit()
            return False
        self.detach_observers()
        # Detaching fences callbacks from the old source permanently. A newly
        # accepted source gets a fresh gate; never clear the old event because
        # an old worker may still hold it.
        self._admission_stopped = threading.Event()
        if self.session is not None:
            try:
                self._sessionChange.disconnect(self._on_session_change)
            except (RuntimeError, TypeError):
                pass
        self._snapshot_source = source if isinstance(source, FilmOptimizationSourceSnapshot) else None
        self._snapshot_owner_token = getattr(self._snapshot_source, "owner_token", None)
        self._source_controller = None if self._snapshot_source is not None else source
        self.session = getattr(source, "session", None) if self._snapshot_source is None else None
        self.service = source.service
        self._detached_source = self._snapshot_source is not None
        self._source_closed = self._observers_detached = self._source_stale = False
        self._configuration_load_error = self._error = ""
        self._configuration_loading = True
        try:
            self._invalidate_result()
            self._queue_job_id = ""
            self._operation_serial += 1
            self._active_operation_id = None
            self._operation_edit_epoch = None
            self._load_source(preserve_variables=False)
        finally:
            self._configuration_loading = False
        self._restore_session_configuration(reset_missing=True)
        self._attach_source_observers()
        self.changed.emit()
        # The source is accepted even if its saved settings need correction;
        # canStart remains false and the exact error is visible.
        return True

    @Slot()
    def detach_observers(self) -> None:
        """Stop receiving source/session events before this window is destroyed."""
        if self._observers_detached:
            return
        if self._busy and self._pending_angle_preparation is not None:
            # Pre-submission preparation is local work even for project-owned
            # windows; disposal must stop it rather than leave a large grid
            # generation running without an observer.
            self._run_coordinator.request_cancel()
        self._admission_stopped.set()
        self._observers_detached = True
        for signal, slot in self._source_connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._source_connections.clear()
        unsubscribe, self._session_unsubscribe = self._session_unsubscribe, None
        if callable(unsubscribe):
            try:
                unsubscribe()
            except (RuntimeError, TypeError):
                pass
        self._source_closed = True
        self._source_stale = True
        if self._busy and self.project_context is None and self._pending_angle_preparation is None:
            self.cancelOptimization()

    def _source_edit_epoch(self) -> int | None:
        if self._source_controller is None:
            return None
        try:
            return int(getattr(self._source_controller, "pendingEditEpoch", 0))
        except (TypeError, ValueError, RuntimeError):
            return None

    @Slot()
    def _on_source_pending_edit(self) -> None:
        self.changed.emit()

    @Slot()
    def markSourceClosed(self) -> None:
        if self._source_closed:
            return
        self.detach_observers()
        self._source_closed = True
        self._source_stale = True
        self.changed.emit()

    def _load_source(self, *, preserve_variables: bool) -> None:
        previous = {(row["side"], row["layer_id"]): row
                    for row in self._variables} if preserve_variables else {}
        if self.session is not None:
            snapshot = self.session.snapshot()
            design = snapshot.design
            design_revision = snapshot.design_revision
            path = snapshot.path
            source_controller = self._source_controller
        else:
            snapshot = self._snapshot_source
            design = snapshot.design
            design_revision = snapshot.design_revision
            path = snapshot.path
            source_controller = None
        self._design = deepcopy(design)
        self._source_revision = int(design_revision)
        self._path = str(path or "")
        self._source_stale = False
        rows_by_side = {}
        for side in ("front", "back"):
            try:
                layer_rows = (snapshot.layers_by_side.get(side, ())
                              if self._detached_source
                              else source_controller.schematicLayers(side))
                rows_by_side[side] = {row.get("id"): row for row in layer_rows}
            except Exception:
                rows_by_side[side] = {}
        variables = []
        for side in ("front", "back"):
            layers = self._design.get(side + "_layers", [])
            for layer in layers:
                layer_id = str(layer.get("id", ""))
                old = previous.get((side, layer_id))
                display = rows_by_side[side].get(layer_id, {})
                physical_nm = display.get("physicalNmValue")
                try:
                    physical_nm = _number(physical_nm, "physical thickness")
                    if physical_nm <= 0:
                        physical_nm = None
                except (TypeError, ValueError):
                    physical_nm = None
                if old is not None:
                    low = old["lower_nm"]
                    high = old["upper_nm"]
                    enabled = bool(old["enabled"])
                elif physical_nm is not None:
                    low = max(0.001, physical_nm * 0.5)
                    high = max(physical_nm * 1.5, low + 0.001)
                    enabled = not preserve_variables
                else:
                    low, high, enabled = 0.0, 0.0, False
                variables.append({
                    "side": side,
                    "side_label": "正面" if side == "front" else "背面",
                    "layer_id": layer_id,
                    "label": str(display.get("name") or layer.get("material_id") or "未命名材料"),
                    "set_name": str(display.get("setName") or layer.get("optical_set_id") or "未选择数据集"),
                    "unit": str(layer.get("thickness", {}).get("unit", "nm")),
                    "physical_nm": physical_nm,
                    "enabled": enabled,
                    "lower_nm": float(low),
                    "upper_nm": float(high),
                    "available": physical_nm is not None,
                })
        if preserve_variables:
            current_ids = {(row["side"], row["layer_id"]) for row in variables}
            orphaned = set(previous) - current_ids
            added = current_ids - set(previous)
            if orphaned or added:
                parts = ["已保留目标、算法和仍匹配的膜层约束。"]
                if orphaned:
                    references = "、".join(f"{side}/{layer_id}" for side, layer_id in sorted(orphaned))
                    parts.append(f"{len(orphaned)} 个旧膜层约束已失效，未转移到其它膜层：{references}。")
                if added:
                    parts.append(f"恢复时未自动勾选 {len(added)} 个新膜层；请在约束页核对选择与厚度范围。")
                self._configuration_restore_notice = "".join(parts)
        self._variables = variables
        self._source_document_replaced = False
        self._source_stale = False
        self._refresh_structure_material_options(preserve_selection=preserve_variables)
        self.changed.emit()
        self.plotDataChanged.emit()

    def _refresh_structure_material_options(self, *, preserve_selection: bool) -> None:
        previous = {(row["material_id"], row["optical_set_id"])
                    for row in self._structure_material_options if row.get("selected")}
        current = {
            (str(layer.get("material_id", "")), str(layer.get("optical_set_id", "")))
            for side in ("front", "back")
            for layer in self._design.get(side + "_layers", [])
            if layer.get("material_id") and layer.get("optical_set_id")
        }
        if not preserve_selection:
            previous = current
        rows: list[dict] = []
        try:
            catalog = getattr(self.service, "materials", None)
            if catalog is None:
                raise ValueError("膜材目录不可用")
            records = catalog._records() if callable(getattr(catalog, "_records", None)) else None
            if records is None:
                raise ValueError("膜材目录不支持枚举")
            for material in records:
                if not isinstance(material, dict) or material.get("design_ready") is False:
                    continue
                material_id = material.get("id")
                sets = material.get("optical_sets")
                roles = material.get("roles", [])
                if isinstance(roles, str):
                    roles = [roles]
                if not isinstance(roles, (list, tuple, set)):
                    roles = []
                if (not isinstance(material_id, str) or not material_id
                        or not isinstance(sets, list)
                        or ("film" not in roles and material.get("category") != "film"
                            and not any((material_id, str(optical.get("id", ""))) in current
                                        for optical in sets if isinstance(optical, dict)))):
                    continue
                for optical in sets:
                    if not isinstance(optical, dict):
                        continue
                    optical_id = optical.get("id")
                    if not isinstance(optical_id, str) or not optical_id:
                        continue
                    pair = (material_id, optical_id)
                    rows.append({
                        "material_id": material_id,
                        "optical_set_id": optical_id,
                        "material_name": str(material.get("name") or material_id),
                        "optical_set_name": str(optical.get("name") or optical_id),
                        "display": (f"{material.get('name') or material_id} · "
                                    f"{optical.get('name') or optical_id}"),
                        "selected": pair in previous,
                    })
            rows.sort(key=lambda row: (row["material_name"].casefold(),
                                       row["optical_set_name"].casefold(),
                                       row["material_id"], row["optical_set_id"]))
            self._structure_material_error = ""
        except Exception as exc:
            rows = []
            self._structure_material_error = f"{type(exc).__name__}: {exc}"
        self._structure_material_options = rows

    def _on_session_change(self, change) -> None:
        if self._configuration_loading:
            return
        snapshot = self.session.snapshot()
        categories = getattr(change, "categories", ())
        document_replaced = (snapshot.design.get("id") != self._design.get("id")
                             or ("workflow_settings" in categories
                                 and snapshot.design_revision != self._source_revision))
        if document_replaced and self._busy:
            self._source_document_replaced = True
            self._source_stale = True
            self.changed.emit()
            return
        if document_replaced and not self._busy:
            self._configuration_loading = True
            try:
                self._invalidate_result()
                self._load_source(preserve_variables=False)
            finally:
                self._configuration_loading = False
            self._restore_session_configuration(reset_missing=True)
        elif "workflow_settings" in categories and not self._busy:
            self._restore_session_configuration(reset_missing=False)
        elif snapshot.design_revision != self._source_revision:
            self._source_stale = True
            self.changed.emit()

    def _invalidate_result(self) -> None:
        if not self._configuration_loading:
            self._configuration_load_error = ""
        self._result = None
        self._structure_result = None
        self._inspection_objectives = None
        self._result_terminal_state = ""
        self._result_application_eligible = False
        self._baseline = {}
        self._frozen_request = None
        self._completion_handoff_done = False
        self._candidate_saved = False
        self._candidate_applied = False
        self._progress = 0.0
        self._progress_iteration = self._progress_evaluations = 0
        self._progress_best_error = None
        self._progress_text = "参数已更改，请重新开始"
        self.plotDataChanged.emit()
        self.changed.emit()
        self._persist_configuration()

    @Property("QVariantList", notify=changed)
    def objectives(self) -> list[dict]:
        return [dict(row, index=index, selected=index == self._selected_objective,
                     display=self._objective_label(row, index))
                for index, row in enumerate(self._objectives)]

    @Property("QVariantMap", notify=changed)
    def selectedObjective(self) -> dict:
        if 0 <= self._selected_objective < len(self._objectives):
            return dict(self._objectives[self._selected_objective])
        return {}

    @Property("QVariantList", notify=changed)
    def variables(self) -> list[dict]:
        return [dict(row) for row in self._variables]

    @Property(str, notify=changed)
    def searchMode(self) -> str:
        return self._search_mode

    @Property("QVariantList", notify=changed)
    def searchModeOptions(self) -> list[dict]:
        return [
            {"id": "fixed_topology", "label": "固定层序优化"},
            {"id": "structure", "label": "结构合成"},
        ]

    @Property(str, notify=changed)
    def structureMode(self) -> str:
        return self._structure_mode

    @Property("QVariantList", notify=changed)
    def structureModeOptions(self) -> list[dict]:
        return [
            {"id": "combined", "label": "针插敏感度 + 层增长"},
            {"id": "needle_only", "label": "针插敏感度"},
            {"id": "growth_only", "label": "有界层增长"},
        ]

    @Property("QVariantList", notify=changed)
    def structureMaterialOptions(self) -> list[dict]:
        return [dict(row) for row in self._structure_material_options]

    @Property(str, notify=changed)
    def structureMaterialError(self) -> str:
        return self._structure_material_error

    @Property("QVariantMap", notify=changed)
    def structureOptions(self) -> dict:
        values = dict(self._structure_options)
        values["growth_steps_text"] = ", ".join(
            f"{value:g}" for value in values["growth_steps_nm"])
        values["growth_phase_fractions_text"] = ", ".join(
            f"{value:g}" for value in values["growth_phase_fractions"])
        return values

    @Property(str, notify=changed)
    def structureConfigurationError(self) -> str:
        return self._structure_configuration_error()

    @Property("QVariantList", notify=changed)
    def plotObjectives(self) -> list[dict]:
        return [{"index": index, "label": self._objective_label(row, index)}
                for index, row in enumerate(self._inspection_objectives or self._objectives)]

    @Property("QVariantList", notify=changed)
    def plotAngles(self) -> list[dict]:
        objectives = self._inspection_objectives or self._objectives
        if not 0 <= self._plot_objective < len(objectives):
            return []
        try:
            angles = self._angle_preview_for_row(objectives[self._plot_objective])
        except ValueError:
            angles = ()
        return [{"value": angle, "label": f"{angle:g}°"} for angle in angles]

    @Property(str, notify=changed)
    def plotAnglesSummary(self) -> str:
        objectives = self._inspection_objectives or self._objectives
        if not 0 <= self._plot_objective < len(objectives):
            return ""
        try:
            count = _angle_sample_count(objectives[self._plot_objective])
        except (TypeError, ValueError, OverflowError):
            return ""
        if count <= _PLOT_ANGLE_OPTION_LIMIT:
            return ""
        angles = self._angle_preview_for_row(objectives[self._plot_objective])
        return (f"角度选择器仅显示 {len(angles)} 个来自真实网格的代表点；"
                f"运行仍使用完整 {count} 个角度样本。")

    @Property(int, notify=changed)
    def selectedPlotObjectiveIndex(self) -> int:
        return max(0, min(self._plot_objective,
                          len(self._inspection_objectives or self._objectives) - 1))

    @Property(int, notify=changed)
    def selectedPlotAngleIndex(self) -> int:
        angles = self.plotAngles
        return next((index for index, row in enumerate(angles)
                     if math.isclose(float(row["value"]), self._plot_angle, abs_tol=1e-10)), 0)

    @Property("QVariantMap", notify=plotDataChanged)
    def plotData(self) -> dict:
        objectives = self._inspection_objectives or self._objectives
        if self._result is None or not 0 <= self._plot_objective < len(objectives):
            return {}
        objective = objectives[self._plot_objective]
        rows = [row for row in self._result.per_condition_errors
                if row.objective_index == self._plot_objective
                and math.isclose(row.angle_deg, self._plot_angle, abs_tol=1e-10)]
        rows.sort(key=lambda row: row.wavelength_nm)
        if not rows:
            return {}
        original = self._baseline.get((self._plot_objective, self._plot_angle), {})
        return {
            "wavelength_nm": [row.wavelength_nm for row in rows],
            "baseline": list(original.get("actual", [])),
            "candidate": [row.actual for row in rows],
            "metric": objective["metric"],
            "mode": objective["mode"],
            "target_value": (float(objective["target_value_percent"]) / 100.0
                             if objective["mode"] == "value" else None),
            "lower_bound": (float(objective["lower_bound_percent"]) / 100.0
                            if objective["mode"] == "range" and str(objective["lower_bound_percent"]).strip() else None),
            "upper_bound": (float(objective["upper_bound_percent"]) / 100.0
                            if objective["mode"] == "range" and str(objective["upper_bound_percent"]).strip() else None),
        }

    @Property("QVariantList", notify=changed)
    def conditionErrors(self) -> list[dict]:
        if self._result is None:
            return []
        rows = sorted(self._result.per_condition_errors,
                      key=lambda row: row.normalized_violation, reverse=True)[:24]
        return [{
            "objective": row.objective_index + 1,
            "wavelength": f"{row.wavelength_nm:.4g} nm",
            "angle": f"{row.angle_deg:g}°",
            "polarization": row.polarization,
            "metric": row.metric,
            "actual": f"{row.actual * 100:.4g}%",
            "target": self._condition_target(row),
            "violation": f"{row.normalized_violation * 100:.4g}%",
            "normalized_violation": row.normalized_violation,
        } for row in rows]

    @Property(str, notify=changed)
    def sourceName(self) -> str:
        return str(self._design.get("name") or "未命名膜系")

    @Property(str, notify=changed)
    def sourcePath(self) -> str:
        return self._path

    @Property(bool, notify=changed)
    def detachedSource(self) -> bool:
        return self._detached_source

    @Property(QUrl, notify=changed)
    def defaultDirectory(self) -> QUrl:
        return QUrl.fromLocalFile(str(Path(self.service.root) / "data" / "film_design"))

    @Property(bool, notify=changed)
    def sourceStale(self) -> bool:
        return self._source_stale

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy

    @Property(float, notify=changed)
    def progress(self) -> float:
        return self._progress

    @Property(str, notify=changed)
    def progressText(self) -> str:
        return self._progress_text

    @Property(str, notify=changed)
    def progressStatistics(self) -> str:
        text = f"迭代 {self._progress_iteration} · 评估 {self._progress_evaluations}"
        if self._progress_best_error is not None:
            text += f" · 最大违约 {self._progress_best_error * 100:.4g}%"
        return text

    @Property(str, notify=changed)
    def queueJobId(self) -> str:
        return self._queue_job_id

    @Property(str, notify=changed)
    def error(self) -> str:
        return self._error

    @Property(str, notify=changed)
    def statusText(self) -> str:
        if self._result_terminal_state in {"已取消", "失败"}:
            if self._result is not None:
                status = (f"优化{self._result_terminal_state}；已保留队列验证的最佳候选，"
                          "可查看或另存，不能应用。")
                return status + (" " + self._error if self._error else "")
            if self._result_terminal_state == "失败" and self._error:
                return f"优化失败；没有可用的验证候选，原设计未改变。{self._error}"
            return "优化已取消；队列没有可用的验证候选，原设计未改变。"
        if self._error:
            return self._error
        if self._source_stale:
            return "原设计已修改；先刷新设计，再开始优化或应用候选。"
        configuration_error = self._active_configuration_error()
        if configuration_error:
            return configuration_error
        if not self._objectives:
            return "至少添加一个目标后才能优化。"
        if (self._search_mode == "fixed_topology"
                and not any(row["enabled"] for row in self._variables)):
            return ("请在约束页选择当前膜层厚度变量；旧约束未转移到新膜层。"
                    if self._configuration_restore_notice
                    else "至少选择一个有效膜层厚度变量。")
        if self._search_mode == "structure":
            configuration_error = self._structure_configuration_error()
            if configuration_error:
                return configuration_error
        if self._busy:
            return self._progress_text
        if self._result is None:
            return "按波段内离散波长和所列角度采样点进行优化。"
        if self._result.state == "cancelled":
            return "优化已取消；最佳候选仍可预览或另存。"
        if self._result.targets_met:
            return "已满足所有采样目标；可预览、另存或显式应用。"
        return "当前候选未满足所有目标；仍可预览或另存。"

    @Property(bool, notify=changed)
    def canStart(self) -> bool:
        has_search_inputs = (
            any(row["enabled"] and row["available"] for row in self._variables)
            if self._search_mode == "fixed_topology"
            else (self._structure_mode == "growth_only"
                  or any(row.get("selected") for row in self._structure_material_options)))
        return (not self._busy and not self._source_closed and not self._source_stale
                and bool(self._objectives) and has_search_inputs
                and not self._pending_edit_error
                and not self._active_configuration_error())

    @Property(bool, notify=changed)
    def canStartFromShortcut(self) -> bool:
        return (not self._busy and not self._source_closed
                and (self.session is not None or self._detached_source))

    def _record_pending_edit_error(self, key: str, message: str) -> None:
        self._pending_edit_error = True
        self._pending_edit_error_key = str(key)
        self._error = str(message)
        self.changed.emit()

    def _pending_edit_allows(self, key: str) -> bool:
        return (not self._pending_edit_error
                or self._pending_edit_error_key == str(key))

    def _clear_pending_edit_error(self, key: str) -> bool:
        if (self._pending_edit_error
                and self._pending_edit_error_key != str(key)):
            return False
        self._pending_edit_error = False
        self._pending_edit_error_key = None
        return True

    @Property(bool, notify=changed)
    def hasCandidate(self) -> bool:
        return self._result is not None

    @Property(bool, notify=changed)
    def targetsMet(self) -> bool:
        return bool(self._result and self._result.targets_met)

    @Property(bool, notify=changed)
    def candidateStale(self) -> bool:
        if self._candidate_applied:
            return False
        epoch_changed = (self._operation_edit_epoch is not None
                         and self._source_edit_epoch() != self._operation_edit_epoch)
        return bool(self._result and (self._source_closed or self._source_stale or epoch_changed
                                     or self._result.design_revision != self._source_revision))

    @Property(bool, notify=changed)
    def canApply(self) -> bool:
        return bool(self.session is not None and not self._source_closed and self._result and not self._busy
                    and not self.candidateStale
                    and not self._candidate_applied
                    and self._result_application_eligible)

    @Property(bool, notify=changed)
    def canSave(self) -> bool:
        return self._result is not None and not self._busy

    @Property(bool, notify=changed)
    def canCancel(self) -> bool:
        return self._busy

    @Property(float, notify=changed)
    def acceptableErrorPercent(self) -> float:
        return self._acceptable_error_percent

    @Property(str, notify=changed)
    def solverId(self) -> str:
        return self._solver_id

    @Property(int, notify=changed)
    def solverIndex(self) -> int:
        return _SOLVER_UI_INDEX[self._solver_id]

    @Property("QVariantList", notify=changed)
    def solverOptions(self) -> list[dict]:
        return [{
            "id": solver_id,
            "label": label,
            "available": not bool(reason := self._solver_unavailable_reason(solver_id)),
            "reason": reason,
        } for solver_id, label, _description, _anchor, _uses_scipy
          in _SOLVER_UI_CHOICES]

    @Property(str, notify=changed)
    def solverDescription(self) -> str:
        _solver_id, _label, description, _anchor, _uses_scipy = (
            _SOLVER_UI_CHOICES[_SOLVER_UI_INDEX[self._solver_id]])
        return description

    @Property(str, notify=changed)
    def solverAvailabilityMessage(self) -> str:
        reason = self._solver_unavailable_reason(self._solver_id)
        if reason:
            return "当前方法不可用：" + reason
        configuration_error = self._active_configuration_error()
        if configuration_error:
            return configuration_error
        if self._scipy_import_error:
            if self._solver_id in {"baseline", "simulated_annealing"}:
                return (self.solverDescription + " · SciPy方法已禁用："
                        + self._scipy_import_error)
            return self._scipy_import_error
        return self.solverDescription

    @Property(QUrl, notify=changed)
    def solverHelpUrl(self) -> QUrl:
        row = _SOLVER_UI_CHOICES[_SOLVER_UI_INDEX[self._solver_id]]
        return self._algorithm_doc_url(row[3])

    @Property(QUrl, constant=True)
    def needleHelpUrl(self) -> QUrl:
        return self._algorithm_doc_url("film-solver-needle")

    @Property(_QVARIANT_PROPERTY_TYPE, notify=changed)
    def maxIterations(self) -> int:
        return self._max_iterations

    @Property(_QVARIANT_PROPERTY_TYPE, notify=changed)
    def maxEvaluations(self) -> int:
        return self._max_evaluations

    # QVariant avoids Qt's signed 32-bit property narrowing. These UI integers
    # stay within JavaScript's exact-integer range at their input boundary.
    @Property(_QVARIANT_PROPERTY_TYPE, notify=changed)
    def seed(self) -> int:
        return self._seed

    @Property(_QVARIANT_PROPERTY_TYPE, notify=changed)
    def validationSamplesMultiplier(self) -> int:
        return self._validation_samples_multiplier

    @Property(_QVARIANT_PROPERTY_TYPE, notify=changed)
    def multistartCount(self) -> int:
        return self._multistart_count

    @Property(_QVARIANT_PROPERTY_TYPE, notify=changed)
    def populationSize(self) -> int:
        return self._population_size

    @Property(bool, notify=changed)
    def showMultistartCount(self) -> bool:
        return self._solver_id == "multistart"

    @Property(bool, notify=changed)
    def showPopulationSize(self) -> bool:
        return self._solver_id == "differential_evolution"

    @Property(str, notify=changed)
    def resultValidationSummary(self) -> str:
        result = self._result
        if result is None:
            return ""
        if not result.validation_complete:
            detail = str(result.validation_message or "没有完整的留出网格验证结果")
            return "留出验证：未完成 · " + detail
        if result.validation_targets_met is True:
            outcome = "达标"
        elif result.validation_targets_met is False:
            outcome = "未达标"
        else:
            outcome = "结果未知"
        error = ("未知" if result.validation_max_normalized_error is None else
                 f"{result.validation_max_normalized_error:.4g}")
        detail = f"留出验证：{outcome} · 最大归一化误差 {error} · 评估 {result.validation_evaluations}"
        if result.validation_message:
            detail += " · " + result.validation_message
        return detail

    @Property(str, notify=changed)
    def resultRunMetadataSummary(self) -> str:
        result = self._result
        if result is None:
            return ""
        method = (_SOLVER_UI_CHOICES[_SOLVER_UI_INDEX[result.solver]][1]
                  if result.solver in _SOLVER_UI_INDEX else result.solver)
        reason = {"target_met": "训练目标达标", "evaluation_budget": "评估预算用尽",
                  "iteration_limit": "迭代预算用尽", "converged": "搜索停止",
                  "cancelled": "已取消", "completed": "已完成", "failed": "失败"}.get(
                      result.termination_reason or result.state, result.termination_reason or result.state)
        return (f"方法 {method} · 随机种子 {result.seed} · 终止 {reason}"
                f" · 失败评估 {result.failed_evaluations} · 留出评估 {result.validation_evaluations}")

    @Property(str, notify=changed)
    def structureResultSummary(self) -> str:
        result = self._structure_result
        if result is None:
            return ""
        counters = result.counters
        audit = result.topology_audit
        original = audit.get("base_layer_ids", []) if isinstance(audit, dict) else []
        final = audit.get("best_layer_ids", []) if isinstance(audit, dict) else []
        additions = result.added_needle_layer_ids
        mode = {"combined": "针插敏感度与层增长", "needle_only": "针插敏感度",
                "growth_only": "层增长"}.get(result.mode, result.mode)
        return (f"模式：{mode}；原层数：{len(original)}；最终层数：{len(final)}；"
                f"新增针层：{len(additions)}；候选数：{counters.candidate_count}；"
                f"目标评分：{counters.score_evaluations}；导数计算：{counters.jacobian_evaluations}；"
                f"验证评分：{counters.validation_evaluations}；搜索记录：{len(result.trace)}")

    @Property(str, notify=changed)
    def resultTerminalState(self) -> str:
        return self._result_terminal_state

    @staticmethod
    def _algorithm_doc_url(anchor: str) -> QUrl:
        path = Path(__file__).resolve().parents[2] / "docs" / "algorithms.html"
        url = QUrl.fromLocalFile(str(path))
        url.setFragment(str(anchor))
        return url

    def _solver_unavailable_reason(self, solver_id: str) -> str:
        if solver_id not in _SOLVER_UI_INDEX:
            return "未知的膜系优化方法"
        if solver_id in _SCIPY_SOLVERS and self._scipy_import_error:
            return self._scipy_import_error
        if solver_id == "trf" and not self._trf_callback_available:
            return "TRF 需要支持 least_squares(callback) 的 SciPy 版本（SciPy 1.16 或更新版本）"
        if solver_id in {"trf", "lbfgsb"}:
            substrate = self._design.get("substrate", {})
            if not isinstance(substrate, dict):
                return "解析梯度方法需要有效的基底配置"
            if substrate.get("sides") != "single":
                return "解析梯度方法仅支持单面基底"
            if self._design.get("first_surface", "front") != "front":
                return "解析梯度方法仅支持 front 为首个表面"
            if substrate.get("mode") not in {"auto", "coherent"}:
                return "解析梯度方法要求基底模式为 auto 或 coherent"
            if any(row.get("enabled") and row.get("side") != "front"
                   for row in self._variables):
                return "解析梯度方法仅支持 front 面厚度变量"
        return ""

    def _active_configuration_error(self) -> str:
        if self._configuration_load_error:
            return self._configuration_load_error
        try:
            training = sum(row["wavelength_samples"] * _angle_sample_count(row)
                           for row in self._objectives)
            validation = sum(((row["wavelength_samples"] - 1) * self._validation_samples_multiplier + 1)
                             * _angle_sample_count(row) for row in self._objectives)
            if training < 1 or validation < 1:
                return "目标波长或角度配置必须至少产生一个计算条件。"
        except (TypeError, ValueError, KeyError):
            return "目标波长或角度配置无效，请修正后再开始。"
        if self._search_mode == "structure":
            return self._structure_configuration_error()
        reason = self._solver_unavailable_reason(self._solver_id)
        if reason:
            return reason
        if self._solver_id == "differential_evolution" and self._population_size < 5:
            return "差分进化的群体数至少为 5"
        if self._pending_edit_error:
            return self._error or "请修正未提交的数字输入"
        return ""

    def _structure_configuration_error(self) -> str:
        if self._pending_edit_error:
            return self._error or "请修正未提交的结构搜索参数"
        if self._structure_material_error:
            return "候选材料目录不可用：" + self._structure_material_error
        if (self._structure_mode != "growth_only"
                and not any(row.get("selected") for row in self._structure_material_options)):
            return "请至少选择一个候选膜材/光学常数组。"
        values = self._structure_options
        try:
            if _number(values["max_total_thickness_nm"], "总厚度上限") <= 0:
                return "总厚度上限必须大于 0 nm。"
            if _number(values["min_thickness_nm"], "单层厚度下限") <= 0:
                return "单层厚度下限必须大于 0 nm。"
            if (_number(values["max_thickness_nm"], "单层厚度上限")
                    <= _number(values["min_thickness_nm"], "单层厚度下限")):
                return "单层厚度上限必须大于下限。"
            max_total_layers = values["max_total_layers"]
            if (isinstance(max_total_layers, bool)
                    or not isinstance(max_total_layers, int)
                    or not 1 <= max_total_layers <= 4096):
                return "总层数预算必须在 1 到 4096 之间（文档解析资源防护）"
            max_added_layers = values["max_added_layers"]
            if (isinstance(max_added_layers, bool)
                    or not isinstance(max_added_layers, int) or max_added_layers != 1):
                return "本轮结构搜索只新增一层候选材料，这是单轮算法范围。"
            position_samples = values["position_samples"]
            if (isinstance(position_samples, bool)
                    or not isinstance(position_samples, int) or position_samples < 1):
                return "层内位置采样数必须为正整数。"
            needle_thickness_samples = values["needle_thickness_samples"]
            if (isinstance(needle_thickness_samples, bool)
                    or not isinstance(needle_thickness_samples, int)
                    or needle_thickness_samples < 1):
                return "针层厚度采样数必须为正整数。"
            candidate_budget = values["candidate_budget"]
            if (isinstance(candidate_budget, bool)
                    or not isinstance(candidate_budget, int) or candidate_budget < 1):
                return "结构候选预算必须为正整数。"
            growth_cycles = values["growth_cycles"]
            if isinstance(growth_cycles, bool) or not isinstance(growth_cycles, int) or growth_cycles != 1:
                return "本轮结构搜索只执行一次层增长循环。"
            if self._max_evaluations < 2:
                return "结构搜索至少需要两次目标评估预算。"
            if not values["growth_steps_nm"]:
                return "至少填写一个有限层增长步长。"
            if not values["growth_phase_fractions"]:
                return "至少填写一个相位增长比例。"
        except (TypeError, ValueError, OverflowError):
            return "结构搜索参数无效。"
        return ""

    @Slot(str, result=bool)
    def setSearchMode(self, mode: str) -> bool:
        mode = str(mode)
        if (self._busy or self._shutting_down or self._pending_edit_error
                or mode not in {"fixed_topology", "structure"}):
            if mode not in {"fixed_topology", "structure"}:
                self._error = "未知的优化模式。"
                self.changed.emit()
            return False
        if mode == self._search_mode:
            return True
        self._search_mode = mode
        self._error = ""
        self._invalidate_result()
        return True

    @Slot(str, result=bool)
    def setStructureMode(self, mode: str) -> bool:
        mode = str(mode)
        allowed = {"combined", "needle_only", "growth_only"}
        if (self._busy or self._shutting_down or self._pending_edit_error
                or mode not in allowed):
            if mode not in allowed:
                self._error = "未知的结构合成模式。"
                self.changed.emit()
            return False
        if mode == self._structure_mode:
            return True
        self._structure_mode = mode
        self._error = ""
        self._invalidate_result()
        return True

    @Slot(str, str, bool, result=bool)
    def setStructureMaterialSelected(
            self, material_id: str, optical_set_id: str, selected: bool) -> bool:
        if self._busy or self._shutting_down or self._pending_edit_error:
            return False
        pair = (str(material_id), str(optical_set_id))
        row = next((item for item in self._structure_material_options
                    if (item["material_id"], item["optical_set_id"]) == pair), None)
        if row is None:
            self._error = "所选候选材料已不在当前材料目录中。"
            self.changed.emit()
            return False
        row["selected"] = bool(selected)
        self._error = ""
        self._invalidate_result()
        return True

    @staticmethod
    def _parse_positive_float_list(value: Any, label: str, *, maximum: float,
                                   allow_fraction: bool = False) -> tuple[float, ...]:
        parts = [part for part in re.split(r"[,，;；\s]+", str(value).strip()) if part]
        if not parts:
            raise ValueError(f"{label}至少需要一个数值")
        numbers = tuple(_number(part, label) for part in parts)
        upper = 2.0 if allow_fraction else maximum
        if any(number <= 0 or number > upper for number in numbers):
            raise ValueError(f"{label}必须大于 0 且不超过 {upper:g}")
        if len(set(numbers)) != len(numbers):
            raise ValueError(f"{label}不能重复")
        return numbers

    @Slot(str, "QVariant", result=bool)
    def setStructureOption(self, name: str, value) -> bool:
        if self._busy or self._shutting_down:
            return False
        error_key = f"structure_option:{name}"
        if not self._pending_edit_allows(error_key):
            return False
        parsed: float | int | tuple[float, ...]
        try:
            if name in {"growth_steps_nm", "growth_phase_fractions"}:
                if name == "growth_steps_nm":
                    parsed = self._parse_positive_float_list(
                        value, "增长步长（nm）", maximum=1.0e6)
                else:
                    parsed = self._parse_positive_float_list(
                        value, "相位增长比例", maximum=1.0, allow_fraction=True)
                if name == "growth_phase_fractions" and any(item > 2 for item in parsed):
                    raise ValueError("相位增长比例必须在 (0, 2] 内")
            elif name in {"max_total_thickness_nm", "min_thickness_nm", "max_thickness_nm"}:
                parsed_thickness = _number(value, name)
                if parsed_thickness <= 0 or parsed_thickness > 1.0e9:
                    raise ValueError(f"{name} 必须大于 0 且不超过 1000000000 nm")
                if (name == "min_thickness_nm"
                        and parsed_thickness >= _number(self._structure_options["max_thickness_nm"], "dmax")):
                    raise ValueError("dmin 必须小于 dmax")
                if (name == "max_thickness_nm"
                        and parsed_thickness <= _number(self._structure_options["min_thickness_nm"], "dmin")):
                    raise ValueError("dmax 必须大于 dmin")
                parsed = parsed_thickness
            elif name in {"max_total_layers", "max_added_layers", "position_samples",
                          "candidate_budget", "needle_thickness_samples",
                          "growth_cycles", "seed"}:
                if isinstance(value, bool):
                    raise ValueError
                if isinstance(value, int):
                    parsed_integer = value
                elif isinstance(value, float):
                    if not math.isfinite(value) or not value.is_integer():
                        raise ValueError
                    parsed_integer = int(value)
                else:
                    raw = str(value).strip()
                    if not re.fullmatch(r"\+?\d+", raw):
                        raise ValueError
                    parsed_integer = int(raw)
                if parsed_integer > _MAX_QML_SAFE_INTEGER:
                    raise ValueError(
                        f"{name}超过 Qt/QML 可精确表示的整数范围（{_MAX_QML_SAFE_INTEGER}）")
                limits = {"max_total_layers": 4096, "max_added_layers": 1,
                          "growth_cycles": 1, "seed": 0xFFFFFFFF}
                minimum = 0 if name == "seed" else 1
                maximum = limits.get(name)
                if parsed_integer < minimum or (maximum is not None and parsed_integer > maximum):
                    if name == "max_total_layers":
                        raise ValueError("总层数必须为 1 到 4096（文档解析资源防护）")
                    if name in {"max_added_layers", "growth_cycles"}:
                        raise ValueError(f"{name} 必须是 1（当前结构搜索轮次范围）")
                    raise ValueError(f"{name} 必须是有效的正整数")
                parsed = parsed_integer
            else:
                self._record_pending_edit_error(error_key, f"未知的结构搜索参数：{name}")
                return False
        except (TypeError, ValueError, OverflowError) as exc:
            self._record_pending_edit_error(
                error_key, str(exc) or f"{name} 必须是有效的有限值")
            return False
        self._structure_options[name] = parsed
        if not self._clear_pending_edit_error(error_key):
            return False
        self._error = ""
        self._invalidate_result()
        return True

    @Slot(str, result=bool)
    def setSolver(self, solver_id: str) -> bool:
        solver_id = str(solver_id)
        if (self._busy or self._shutting_down
                or self._pending_edit_error):
            return False
        if solver_id not in _SOLVER_UI_INDEX:
            self._error = "未知的膜系优化方法。"
            self.changed.emit()
            return False
        reason = self._solver_unavailable_reason(solver_id)
        if reason:
            self._error = "当前方法不可用：" + reason
            self.changed.emit()
            return False
        if solver_id == self._solver_id:
            self._error = ""
            self.changed.emit()
            return True
        self._solver_id = solver_id
        self._error = ""
        self._invalidate_result()
        return True

    @Slot(str, "QVariant", result=bool)
    def setSolverOption(self, name: str, value) -> bool:
        if self._busy or self._shutting_down:
            return False
        error_key = f"solver_option:{name}"
        if not self._pending_edit_allows(error_key):
            return False
        ranges = {
            "max_iterations": (1, None, "最大迭代数"),
            "max_evaluations": (1, None, "最大评估数"),
            "seed": (0, 0xFFFFFFFF, "随机种子"),
            "validation_samples_multiplier": (1, None, "留出验证倍率"),
            "multistart_count": (1, None, "Powell 起点数"),
            "population_size": (1, None, "差分进化群体数"),
        }
        if name not in ranges:
            self._record_pending_edit_error(
                error_key, f"未知的优化参数：{name}")
            return False
        low, high, label = ranges[name]
        try:
            if isinstance(value, bool):
                raise ValueError
            if isinstance(value, int):
                number = value
            elif isinstance(value, float):
                if not math.isfinite(value) or not value.is_integer():
                    raise ValueError
                number = int(value)
            else:
                raw = str(value).strip()
                if not re.fullmatch(r"\+?\d+", raw):
                    raise ValueError
                number = int(raw)
            if number > _MAX_QML_SAFE_INTEGER:
                raise ValueError(
                    f"{label}超过 Qt/QML 可精确表示的整数范围（{_MAX_QML_SAFE_INTEGER}）")
            if number < low or (high is not None and number > high):
                raise ValueError
        except (TypeError, ValueError, OverflowError) as exc:
            if "Qt/QML" in str(exc):
                self._record_pending_edit_error(error_key, str(exc))
                return False
            self._record_pending_edit_error(
                error_key, (f"{label}必须是大于等于 {low} 的整数"
                            if high is None else f"{label}必须是 {low} 到 {high} 之间的整数"))
            return False
        attr = {
            "max_iterations": "_max_iterations",
            "max_evaluations": "_max_evaluations",
            "seed": "_seed",
            "validation_samples_multiplier": "_validation_samples_multiplier",
            "multistart_count": "_multistart_count",
            "population_size": "_population_size",
        }[name]
        setattr(self, attr, number)
        if not self._clear_pending_edit_error(error_key):
            return False
        self._error = ""
        self._invalidate_result()
        return True

    @Property(str, notify=changed)
    def resultSummary(self) -> str:
        if self._result is None:
            return "尚无候选结果"
        result = self._result
        attain = "已达标" if result.targets_met else "未达标"
        return (f"{attain} · 最大归一化误差 {result.max_normalized_error:.4g} · "
                f"迭代 {result.iterations} · 评估 {result.evaluations}")

    @Property(str, notify=changed)
    def resultMessage(self) -> str:
        return "" if self._result is None else str(self._result.message)

    @Property("QVariantList", notify=changed)
    def bestThicknesses(self) -> list[dict]:
        if self._result is None:
            return []
        labels = {(row["side"], row["layer_id"]): row for row in self._variables}
        output = []
        for (side, layer_id), row in labels.items():
            if layer_id in self._result.best_thickness_nm:
                output.append({
                    "label": f"{row['side_label']} · {row['label']}",
                    "thickness": f"{self._result.best_thickness_nm[layer_id]:.6g} nm",
                })
        return output

    @staticmethod
    def _objective_label(row: dict, index: int) -> str:
        try:
            if row.get("angle_sampling_mode", "points") == "range":
                resolved = _resolve_angle_sampling(row)
                if resolved["angle_min_deg"] == resolved["angle_max_deg"]:
                    angles = f"{resolved['angle_min_deg']:g}°"
                else:
                    angles = (f"{resolved['angle_min_deg']:g}–{resolved['angle_max_deg']:g}° / "
                              f"步长 {resolved['angle_step_deg']:g}°"
                              f"（{resolved['angle_sample_count']} 点）")
            else:
                angles = ", ".join(f"{v:g}°" for v in FilmOptimizationQuickController._parse_angles(
                    str(row.get("angles_text", ""))))
        except ValueError:
            angles = str(row.get("angles_text", ""))
        low = row.get("wavelength_min_nm", "?")
        high = row.get("wavelength_max_nm", "?")
        try:
            low_number = float(low)
            high_number = float(high)
            wavelength = (f"{low_number:g} nm" if low_number == high_number else
                          f"{low}–{high} nm")
        except (TypeError, ValueError, OverflowError):
            wavelength = f"{low}–{high} nm"
        return (f"{index + 1}. {row.get('metric', 'R')} · "
                f"{wavelength} · {angles}")

    @staticmethod
    def _parse_angles(value: str) -> tuple[float, ...]:
        parts = [part for part in re.split(r"[,，;；\s]+", str(value).strip()) if part]
        if not parts:
            raise ValueError("请填写至少一个入射角（度），多个角度用逗号分隔")
        angles = tuple(_number(part, "入射角") for part in parts)
        if len(set(angles)) != len(angles):
            raise ValueError("入射角列表不能重复")
        if any(angle < 0 or angle >= 90 for angle in angles):
            raise ValueError("入射角必须在 0 到小于 90 度之间")
        return angles

    @staticmethod
    def _angles_for_row(row: dict) -> tuple[float, ...]:
        return _angles_for_row_cancellable(row)

    @staticmethod
    def _angle_preview_for_row(row: dict) -> tuple[float, ...]:
        """Return bounded plot choices without truncating the frozen request grid."""
        resolved = _resolve_angle_sampling(row)
        count = int(resolved["angle_sample_count"])
        if resolved["angle_sampling_mode"] == "points":
            values = FilmOptimizationQuickController._parse_angles(
                str(resolved.get("angles_text", "")))
            if count <= _PLOT_ANGLE_OPTION_LIMIT:
                return values
            indices = sorted({round(position * (count - 1) /
                                    (_PLOT_ANGLE_OPTION_LIMIT - 1))
                              for position in range(_PLOT_ANGLE_OPTION_LIMIT)})
            return tuple(values[index] for index in indices)
        if count <= _PLOT_ANGLE_OPTION_LIMIT:
            return FilmOptimizationQuickController._angles_for_row(row)
        indices = sorted({round(position * (count - 1) /
                                (_PLOT_ANGLE_OPTION_LIMIT - 1))
                          for position in range(_PLOT_ANGLE_OPTION_LIMIT)})
        low = float(resolved["angle_min_deg"])
        high = float(resolved["angle_max_deg"])
        actual = (high - low) / (count - 1)
        return tuple(high if index == count - 1 else low + index * actual
                     for index in indices)

    @staticmethod
    def _condition_target(row) -> str:
        if row.target_value is not None:
            return f"目标 {row.target_value * 100:.4g}%"
        low = "无下限" if row.lower_bound is None else f"≥{row.lower_bound * 100:.4g}%"
        high = "无上限" if row.upper_bound is None else f"≤{row.upper_bound * 100:.4g}%"
        return f"{low}，{high}"

    @Slot(int)
    def selectObjective(self, index: int) -> None:
        if (not self._busy and not self._pending_edit_error
                and 0 <= int(index) < len(self._objectives)):
            self._selected_objective = int(index)
            self.changed.emit()

    @Slot()
    def addObjective(self) -> None:
        if self._busy or self._shutting_down or self._pending_edit_error:
            return
        self._objectives.append(_objective_default(len(self._objectives)))
        self._selected_objective = len(self._objectives) - 1
        self._invalidate_result()

    @Slot(result=bool)
    def duplicateSelectedObjective(self) -> bool:
        if (self._busy or self._shutting_down or self._pending_edit_error
                or not 0 <= self._selected_objective < len(self._objectives)):
            return False
        row = deepcopy(self._objectives[self._selected_objective])
        row["id"] = uuid4().hex
        row["name"] = (str(row.get("name", "目标")) + " 副本")
        self._objectives.insert(self._selected_objective + 1, row)
        self._selected_objective += 1
        self._invalidate_result()
        return True

    @Slot()
    def removeSelectedObjective(self) -> None:
        if self._busy or self._shutting_down or self._pending_edit_error:
            return
        if not 0 <= self._selected_objective < len(self._objectives):
            return
        self._objectives.pop(self._selected_objective)
        self._selected_objective = min(self._selected_objective,
                                       len(self._objectives) - 1)
        self._plot_objective = min(self._plot_objective,
                                   max(0, len(self._objectives) - 1))
        self._invalidate_result()

    @Slot(str, "QVariant", result=bool)
    def setObjectiveField(self, field: str, value) -> bool:
        if self._busy or self._shutting_down:
            return False
        if not 0 <= self._selected_objective < len(self._objectives):
            return False
        error_key = f"objective:{self._selected_objective}:{field}"
        if not self._pending_edit_allows(error_key):
            return False
        allowed = {
            "name", "wavelength_min_nm", "wavelength_max_nm", "wavelength_samples",
            "wavelength_step_nm", "angle_sampling_mode", "angle_min_deg", "angle_max_deg",
            "angle_step_deg", "angles_text", "polarization", "metric", "mode",
            "target_value_percent", "lower_bound_percent", "upper_bound_percent", "weight",
        }
        if field not in allowed:
            return False
        row = self._objectives[self._selected_objective]
        if (field == "angle_sampling_mode" and str(value) == "points"
                and row.get("angle_sampling_mode") == "range"):
            try:
                count = _angle_sample_count(row)
            except (TypeError, ValueError, OverflowError):
                count = 0
            if count > _ANGLE_TEXT_CONVERSION_LIMIT:
                self._error = (
                    f"当前范围含 {count} 个角度，无法在界面编辑框中自动展开为显式点列；"
                    "这是界面编辑文本限制，不限制范围模式的完整角度优化。"
                    "请保留范围模式，或先缩小范围/调大步长。")
                self.changed.emit()
                return False
        if field in ("polarization",) and value not in ("s", "p", "unpolarized"):
            return False
        if field == "metric" and value not in ("R", "T"):
            return False
        if field == "mode" and value not in ("value", "range"):
            return False
        updated = deepcopy(row)
        try:
            if field == "wavelength_samples":
                value = _integer(value, "波长采样点数", 1)
            elif field in ("polarization", "metric", "mode", "angle_sampling_mode"):
                value = str(value)
            elif field not in ("name", "angles_text"):
                value = _number(value, {
                    "wavelength_min_nm": "波长下限", "wavelength_max_nm": "波长上限",
                    "wavelength_step_nm": "采样步长", "target_value_percent": "目标值（%）",
                    "lower_bound_percent": "目标下限（%）", "upper_bound_percent": "目标上限（%）",
                    "weight": "目标权重",
                }.get(field, field))
            updated[field] = str(value).strip() if field in ("name", "angles_text") else value
            if field in {"wavelength_min_nm", "wavelength_max_nm", "wavelength_samples", "wavelength_step_nm"}:
                updated = _resolve_objective_sampling(updated, edited_field=field)
            if field in {"angle_min_deg", "angle_max_deg", "angle_step_deg", "angle_sampling_mode"}:
                if field == "angle_sampling_mode" and value == "range":
                    current = self._parse_angles(str(row.get("angles_text", "0")))
                    updated["angle_min_deg"] = min(current)
                    updated["angle_max_deg"] = max(current)
                    updated["angle_step_deg"] = (
                        (max(current) - min(current)) / (len(current) - 1)
                        if len(current) > 1 else 1.0)
                elif field == "angle_sampling_mode" and value == "points":
                    if row.get("angle_sampling_mode") == "range":
                        current_angles = self._angles_for_row(row)
                        updated["angles_text"] = ", ".join(
                            format(angle, ".17g") for angle in current_angles)
                    else:
                        updated["angles_text"] = str(row.get("angles_text", "0"))
                updated = _resolve_angle_sampling(updated)
        except (TypeError, ValueError, OverflowError, MemoryError) as exc:
            if isinstance(exc, MemoryError):
                exc = ValueError("入射角列表过大，无法转换为显式点列")
            self._record_pending_edit_error(error_key, str(exc))
            return False
        self._objectives[self._selected_objective] = updated
        if not self._clear_pending_edit_error(error_key):
            return False
        self._error = ""
        self._invalidate_result()
        return True

    @Slot(float, result=bool)
    def setAcceptableErrorPercent(self, value: float) -> bool:
        if self._busy or self._shutting_down:
            return False
        error_key = "acceptable_error_percent"
        if not self._pending_edit_allows(error_key):
            return False
        try:
            number = _number(value, "误差容限")
            if number < 0 or number > 100:
                raise ValueError("误差容限必须在 0 到 100 个百分点之间")
            self._acceptable_error_percent = number
        except ValueError as exc:
            self._record_pending_edit_error(error_key, str(exc))
            return False
        if not self._clear_pending_edit_error(error_key):
            return False
        self._error = ""
        self._invalidate_result()
        return True

    @Slot(str, str, bool)
    def setVariableEnabled(self, side: str, layer_id: str, enabled: bool) -> None:
        if self._busy or self._shutting_down or self._pending_edit_error:
            return
        row = self._find_variable(side, layer_id)
        if row is None or not row["available"]:
            return
        row["enabled"] = bool(enabled)
        self._error = ""
        self._invalidate_result()

    @Slot(str, str, str, "QVariant")
    def setVariableBound(self, side: str, layer_id: str, field: str, value) -> bool:
        if self._busy or self._shutting_down:
            return False
        error_key = f"variable:{side}:{layer_id}:{field}"
        if not self._pending_edit_allows(error_key):
            return False
        row = self._find_variable(side, layer_id)
        if row is None or field not in ("lower_nm", "upper_nm"):
            return False
        try:
            row[field] = _number(value, "厚度下限（nm）" if field == "lower_nm" else "厚度上限（nm）")
        except ValueError as exc:
            self._record_pending_edit_error(error_key, str(exc))
            return False
        if not self._clear_pending_edit_error(error_key):
            return False
        self._error = ""
        self._invalidate_result()
        return True

    def _find_variable(self, side: str, layer_id: str) -> dict | None:
        return next((row for row in self._variables
                     if row["side"] == side and row["layer_id"] == layer_id), None)

    @Slot()
    def refreshDesign(self) -> None:
        if self._busy or self._pending_edit_error:
            return
        if self._source_document_replaced:
            self._configuration_loading = True
            try:
                self._invalidate_result()
                self._load_source(preserve_variables=False)
            finally:
                self._configuration_loading = False
            self._restore_session_configuration(reset_missing=True)
            return
        self._load_source(preserve_variables=True)
        self._error = ""
        self._invalidate_result()

    @Slot(object, result=bool)
    def refreshDetachedSource(self, source_snapshot: object) -> bool:
        """Refresh a detached optimizer from its owning simulation snapshot."""
        if (not self._detached_source or self._busy or self._pending_edit_error
                or not isinstance(source_snapshot, FilmOptimizationSourceSnapshot)
                or self._snapshot_owner_token is None
                or source_snapshot.owner_token is not self._snapshot_owner_token):
            return False
        if source_snapshot is self._snapshot_source:
            return True
        document_replaced = source_snapshot.design.get("id") != self._design.get("id")
        self._snapshot_source = source_snapshot
        self.service = source_snapshot.service
        self._configuration_loading = document_replaced
        try:
            self._load_source(preserve_variables=not document_replaced)
            self._error = ""
            self._invalidate_result()
        finally:
            self._configuration_loading = False
        if document_replaced:
            self._restore_session_configuration(reset_missing=True)
        return True

    @Slot(int)
    def selectPlotObjective(self, index: int) -> None:
        if not 0 <= int(index) < len(self._inspection_objectives or self._objectives):
            return
        self._plot_objective = int(index)
        angles = self.plotAngles
        self._plot_angle = float(angles[0]["value"]) if angles else 0.0
        self.changed.emit()
        self.plotDataChanged.emit()

    @Slot(int)
    def selectPlotAngle(self, index: int) -> None:
        angles = self.plotAngles
        if 0 <= int(index) < len(angles):
            self._plot_angle = float(angles[int(index)]["value"])
            self.plotDataChanged.emit()

    def _build_request(self, *, require_variables: bool = True,
                       fixed_topology: bool = True,
                       prepared_angles: tuple[tuple[float, ...], ...] | None = None
                       ) -> FilmOptimizationRequest:
        configuration_error = self._active_configuration_error()
        if configuration_error:
            raise ValueError(configuration_error)
        snapshot = self.session.snapshot() if self.session is not None else None
        if snapshot is not None and snapshot.design_revision != self._source_revision:
            raise ValueError("原设计已修改；请先刷新设计快照")
        if prepared_angles is not None and len(prepared_angles) != len(self._objectives):
            raise ValueError("后台准备的角度网格与当前目标数不一致")
        variables = []
        for row in self._variables:
            if not row["enabled"]:
                continue
            if not row["available"]:
                raise ValueError(f"膜层 {row['layer_id']} 的厚度或光学数据不可用")
            variables.append(FilmThicknessVariable(
                row["side"], row["layer_id"],
                _number(row["lower_nm"], "下限 nm"),
                _number(row["upper_nm"], "上限 nm"),
            ))
        objectives = []
        for index, row in enumerate(self._objectives):
            low = _number(row["wavelength_min_nm"], f"目标 {index + 1} 波长下限")
            high = _number(row["wavelength_max_nm"], f"目标 {index + 1} 波长上限")
            samples = int(row["wavelength_samples"])
            if low <= 0:
                raise ValueError(f"目标 {index + 1} 波长下限必须是正数（nm）")
            if high < low:
                raise ValueError(
                    f"目标 {index + 1} 波长上限不能小于下限（nm）")
            if samples < 1 or (low != high and samples < 2):
                raise ValueError(
                    f"目标 {index + 1} 非零波段需要至少 2 点，单波长需要 1 点")
            if high == low:
                samples = 1
            angles = (self._angles_for_row(row) if prepared_angles is None
                      else tuple(prepared_angles[index]))
            mode = row["mode"]
            objectives.append(FilmOptimizationObjective(
                wavelength_min_nm=low,
                wavelength_max_nm=high,
                wavelength_samples=samples,
                angles_deg=angles,
                polarization=row["polarization"],
                metric=row["metric"],
                mode=mode,
                target_value=(float(row["target_value_percent"]) / 100.0
                              if mode == "value" else None),
                lower_bound=(float(row["lower_bound_percent"]) / 100.0
                             if mode == "range" and str(row["lower_bound_percent"]).strip() else None),
                upper_bound=(float(row["upper_bound_percent"]) / 100.0
                             if mode == "range" and str(row["upper_bound_percent"]).strip() else None),
                weight=_number(row["weight"], f"目标 {index + 1} 权重"),
                normalization_scale=1.0,
            ))
        selected = [row for row in self._variables if row["enabled"]]
        if not objectives:
            raise ValueError("至少添加一个目标条件")
        if require_variables and not selected:
            raise ValueError("至少选择一个有效膜层厚度变量")
        acceptable = _number(self._acceptable_error_percent, "误差容限") / 100.0
        design = deepcopy(self._design if snapshot is None else snapshot.design)
        design["workflow_settings"] = {**design.get("workflow_settings", {}),
                                       "optimization": self.exportOptimizationConfiguration()}
        return FilmOptimizationRequest(
            design_snapshot=design,
            design_revision=int(self._source_revision if snapshot is None
                                else snapshot.design_revision),
            variables=tuple(variables),
            objectives=tuple(objectives),
            max_iterations=self._max_iterations,
            max_evaluations=self._max_evaluations,
            acceptable_normalized_error=acceptable,
            solver=self._solver_id if fixed_topology else "baseline",
            seed=self._seed if fixed_topology else 0,
            multistart_count=self._multistart_count,
            population_size=self._population_size,
            validation_samples_multiplier=self._validation_samples_multiplier,
        )

    def _build_structure_request(self, *,
                                 prepared_angles: tuple[tuple[float, ...], ...] | None = None):
        from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
        from coat_sim.core.film_optimization_structure import FilmStructureOptimizationRequest

        configuration_error = self._structure_configuration_error()
        if configuration_error:
            raise ValueError(configuration_error)
        selected = (() if self._structure_mode == "growth_only" else tuple(
            FilmNeedleMaterialReference(row["material_id"], row["optical_set_id"])
            for row in self._structure_material_options if row.get("selected")))
        if self._structure_mode != "growth_only" and not selected:
            raise ValueError("请至少选择一个候选膜材/光学常数组。")
        values = self._structure_options
        return FilmStructureOptimizationRequest(
            base_request=self._build_request(require_variables=False,
                                             fixed_topology=False,
                                             prepared_angles=prepared_angles),
            candidate_materials=selected,
            mode=self._structure_mode,
            max_total_thickness_nm=float(values["max_total_thickness_nm"]),
            min_thickness_nm=float(values["min_thickness_nm"]),
            max_thickness_nm=float(values["max_thickness_nm"]),
            max_total_layers=_integer(values["max_total_layers"], "总层数预算", 1, 4096),
            max_added_layers=_integer(values["max_added_layers"], "新增层数", 1, 1),
            position_samples=_integer(values["position_samples"], "层内位置采样数", 1, None),
            candidate_budget=_integer(values["candidate_budget"], "结构候选预算", 1, None),
            needle_thickness_samples=_integer(
                values["needle_thickness_samples"], "针层厚度采样数", 1, None),
            growth_steps_nm=cast(tuple[float, ...], values["growth_steps_nm"]),
            growth_phase_fractions=cast(
                tuple[float, ...], values["growth_phase_fractions"]),
            growth_cycles=_integer(values["growth_cycles"], "层增长循环数", 1, 1),
            seed=_integer(values["seed"], "随机种子", 0, 0xFFFFFFFF),
        )

    @Slot(result=bool)
    def startOptimization(self) -> bool:
        return self._start_optimization()

    def _requires_async_angle_preparation(self) -> bool:
        total = 0
        for row in self._objectives:
            total += _angle_sample_count(row)
            if total > _ANGLE_ASYNC_PREPARATION_THRESHOLD:
                return True
        return False

    def _start_angle_preparation(self, request_template) -> bool:
        """Expand, freeze, and serialize large grids before queue submission."""
        rows = tuple(deepcopy(row) for row in self._objectives)
        revision = int(self._source_revision)
        edit_epoch = self._source_edit_epoch()
        operation_id = self._run_coordinator.operation_serial + 1
        cancel_event = threading.Event()
        job_id = uuid4().hex
        if self._project_compute is not None:
            try:
                self._project_operation = self._project_compute.capture(job_id)
            except Exception as exc:
                self._error = f"项目已关闭或正在退出；未准备角度网格：{exc}"
                self.changed.emit()
                return False
        self._pending_angle_preparation = {
            "operation_id": operation_id,
            "design_revision": revision,
            "edit_epoch": edit_epoch,
            "job_id": job_id,
        }
        try:
            self._run_coordinator.start(
                submit=None,
                worker_factory=lambda current_id: _AngleGridPreparationTask(
                    rows, request_template, self._search_mode, self.service,
                    cancel_event, current_id),
                edit_epoch=edit_epoch,
            )
        except Exception:
            self._pending_angle_preparation = None
            raise
        self._error = ""
        self._progress = 0.0
        self._progress_text = "正在后台准备角度采样网格；尚未提交队列作业"
        self.changed.emit()
        return True

    def _start_optimization(self, *, prepared_start: _PreparedAngleGrid | None = None
                            ) -> bool:
        if self._pending_reuse_source is not None:
            self._pending_reuse_start = True
            return True
        if self._busy or self._shutting_down:
            return False
        if self._source_closed:
            self._error = "原设计窗口已关闭；不能开始或回写优化。"
            self.changed.emit()
            return False
        if prepared_start is None:
            try:
                configuration_error = self._active_configuration_error()
                if configuration_error:
                    raise ValueError(configuration_error)
                if self._requires_async_angle_preparation():
                    placeholders = tuple((0.0,) for _ in self._objectives)
                    request_template = (
                        self._build_structure_request(prepared_angles=placeholders)
                        if self._search_mode == "structure"
                        else self._build_request(prepared_angles=placeholders))
                    return self._start_angle_preparation(request_template)
            except Exception as exc:
                self._error = str(exc)
                self.changed.emit()
                return False
        try:
            if prepared_start is not None:
                request = prepared_start.request
            elif self._search_mode == "structure":
                request = self._build_structure_request()
            else:
                request = self._build_request()
        except (TypeError, ValueError, OverflowError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        async_prepared = prepared_start is not None
        try:
            store, _device = self._compute_target.client_for_submit(
                client_factory=SchedulerClient)
            remote = bool(getattr(getattr(store, "transport", None), "remote_http", False))
            if not remote:
                try:
                    capabilities = store.capabilities()
                except Exception as exc:
                    raise _QueueCompatibilityError(
                        "当前后台队列无法报告执行器能力；未提交优化任务。"
                        f"队列诊断：{exc}") from exc
                if (not isinstance(capabilities, dict) or
                        capabilities.get("protocol_version") != 1 or
                        not isinstance(capabilities.get("executors"), (list, tuple))):
                    raise _QueueCompatibilityError(
                        "当前后台队列未提供兼容的能力信息；未提交优化任务。"
                        "请在安全时重启桌面程序后重试。")
                if "film_optimization" not in capabilities["executors"]:
                    raise _QueueCompatibilityError(
                        "当前后台队列不支持膜系优化；未提交优化任务。"
                        "请在安全时重启桌面程序后重试。")
        except _QueueCompatibilityError as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        except Exception as exc:
            self._error = (
                "当前后台队列无法报告执行器能力；未提交优化任务。"
                f"队列诊断：{exc}")
            self.changed.emit()
            return False
        try:
            # Freeze the design and referenced optical records only after the
            # running queue confirms it can accept this executor.
            if prepared_start is None:
                if self._search_mode == "structure":
                    from coat_sim.core.film_optimization_structure import (
                        freeze_film_structure_optimization_request,
                    )

                    request = freeze_film_structure_optimization_request(self.service, request)
                else:
                    request = freeze_film_optimization_request(self.service, request)
        except (TypeError, ValueError, OverflowError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        try:
            spec = (prepared_start.spec if prepared_start is not None else
                    freeze_spec(self.service, "film_optimization", {"request": request}))
            prepared_angle_job_id = self._prepared_angle_job_id
            job_id = prepared_angle_job_id or uuid4().hex
            self._prepared_angle_job_id = None
            if self._project_compute is not None:
                if self._project_compute.editing_frozen:
                    raise ValueError("项目已关闭或正在退出；未提交优化任务。")
                if prepared_angle_job_id is None:
                    self._project_operation = self._project_compute.capture(job_id)
                elif not self._project_current():
                    raise ValueError("项目已关闭或正在退出；未提交优化任务。")
            base_request = cast(FilmOptimizationRequest, getattr(request, "base_request", request))
            metadata = {
                "summary": ("Film structure synthesis — "
                            if self._search_mode == "structure"
                            else "Film optimization — ") + str(
                    base_request.design_snapshot.get("name", "film design")),
                "origin": {"surface": "desktop"},
                "input_summary": {
                    "design_name": str(base_request.design_snapshot.get("name", "")),
                    "design_revision": base_request.design_revision,
                    "objective_count": len(base_request.objectives),
                    "variable_count": len(base_request.variables),
                    "search_mode": self._search_mode,
                    "structure_mode": (request.mode if self._search_mode == "structure" else None),
                    "candidate_material_count": (len(request.candidate_materials)
                                                  if self._search_mode == "structure" else 0),
                },
            }
            if self._project_compute is not None:
                if self._project_operation is None:
                    raise ValueError("Project optimization is missing its frozen operation")
                metadata = self._project_compute.metadata(self._project_operation, metadata)
            cancel_event = (_LocalAdmissionCancelEvent(store, job_id)
                            if async_prepared and not remote else
                            RemoteCancelEvent(store, job_id))
            admission_project = self._project_compute
            admission_operation = self._project_operation
            admission_stopped = self._admission_stopped

            def admission_project_is_current() -> bool:
                if admission_stopped.is_set():
                    return False
                if admission_project is None:
                    return True
                return (not admission_project.editing_frozen
                        and admission_operation is not None
                        and admission_project.accepts(admission_operation))

            remote_admission = None
            if remote:
                device_snapshot = deepcopy(dict(_device))
                transport_device_id = getattr(
                    getattr(store, "transport", None), "device_id", None)
                if transport_device_id != device_snapshot.get("id"):
                    raise _QueueCompatibilityError(
                        "远程请求设备与冻结的设备选择不一致；未提交任务。")

                def remote_admission():
                    if not admission_project_is_current():
                        raise InterruptedError("项目已关闭或正在退出；未接纳新的远程作业。")
                    from coat_sim.ui.compute_target import validate_capabilities
                    from coat_sim.scheduler.remote_client import (
                        RemoteSubmissionUnknown,
                    )

                    try:
                        capabilities = store.capabilities()
                    except Exception as exc:
                        raise _QueueCompatibilityError(
                            "远程主机能力核对失败；未提交优化任务。") from exc
                    try:
                        rows = validate_capabilities(
                            capabilities, "film_optimization", remote=True)
                    except Exception as exc:
                        raise _QueueCompatibilityError(
                            f"远程主机能力不兼容；未提交优化任务（{getattr(exc, 'code', 'invalid_capabilities')}）。"
                        ) from exc
                    selected = next((row for row in rows
                                     if row.get("id") == device_snapshot.get("id")), None)
                    if selected is None or not selected.get("available"):
                        raise _QueueCompatibilityError(
                            "冻结的远程设备当前不可用；未提交优化任务。")
                    if selected.get("backend") != "CPU":
                        raise _QueueCompatibilityError(
                            "膜系优化只允许绑定 CPU 设备；未提交任务。")
                    if cancel_event.is_set():
                        raise InterruptedError(
                            "已在远程任务接纳前取消；未创建作业。")
                    if not admission_project_is_current():
                        raise InterruptedError("项目已关闭或正在退出；未接纳新的远程作业。")
                    try:
                        store.submit(spec, job_id=job_id, metadata=metadata)
                    except RemoteSubmissionUnknown as unknown:
                        try:
                            resolve_unknown_submission(store, unknown)
                        except RemoteSubmissionUnknown:
                            raise
                        except Exception:
                            # A mismatched or unavailable receipt cannot
                            # authorize a second POST. Keep the same ID unknown.
                            raise unknown from None
                        cancel_event.replace_job_id_before_admission(unknown.job_id)
                        cancel_event.mark_admitted()
                        return
                    except Exception as exc:
                        http_status = getattr(exc, "http_status", None)
                        if http_status is None or not (400 <= http_status < 500):
                            # Anything other than a definite client rejection
                            # may have committed; fail closed on this exact ID.
                            raise RemoteSubmissionUnknown(job_id) from None
                        raise
                    cancel_event.mark_admitted()

            async_local_admission = None
            if async_prepared and not remote:
                local_cancel_event = cast(_LocalAdmissionCancelEvent, cancel_event)

                def async_local_admission():
                    if local_cancel_event.cancel_intent.is_set():
                        raise InterruptedError(
                            "已在本地作业接纳前取消；未创建作业。")
                    if not admission_project_is_current():
                        raise InterruptedError("项目已关闭或正在退出；未接纳新的优化作业。")
                    store.submit(spec, job_id=job_id, metadata=metadata)
                    local_cancel_event.mark_admitted()
                    cancel_warning = ""
                    # Cancellation can race the local submit. Retry against
                    # this exact ID only after the queue row is accepted.
                    if (local_cancel_event.cancel_intent.is_set()
                            and not local_cancel_event.is_set()):
                        try:
                            local_cancel_event.set()
                        except Exception as exc:
                            # Keep the base event unset so a later click can
                            # retry the now-admitted local cancellation.
                            cancel_warning = (
                                "队列已接纳，但首次取消请求失败（"
                                f"{type(exc).__name__}: {exc}）；请再次点击取消重试。")
                    return cancel_warning

            def create_worker(operation_id):
                args: tuple[Any, FilmOptimizationRequest, Any, str, RemoteCancelEvent, int] = (
                    self.service, base_request, store, job_id, cancel_event, operation_id)
                queue_admission = remote_admission
                if queue_admission is None:
                    queue_admission = async_local_admission
                return (_FilmOptimizationTask(*args, remote_admission=queue_admission,
                                              project_compute=self._project_compute)
                        if queue_admission is not None else
                        _FilmOptimizationTask(*args, project_compute=self._project_compute))

            def submit_job():
                store.submit(spec, job_id=job_id, metadata=metadata)
                if self._project_compute is not None:
                    self._project_compute.register_job(
                        job_id, module_key="film_optimization", service_owned=True,
                        metadata=metadata)
                self._queue_job_id = job_id

            previous_job_id = self._queue_job_id
            if remote or async_prepared:
                self._queue_job_id = job_id
                if self._project_compute is not None:
                    self._project_compute.register_job(
                        job_id, module_key="film_optimization", service_owned=False,
                        state="submitting", metadata=metadata)
            self._run_coordinator.start(
                submit=None if (remote or async_prepared) else submit_job,
                worker_factory=create_worker,
                edit_epoch=self._source_edit_epoch(),
            )
        except Exception as exc:
            if self._project_compute is not None and "job_id" in locals():
                self._project_compute.update_job(job_id, "failed")
            if locals().get("remote", False):
                self._queue_job_id = locals().get("previous_job_id", self._queue_job_id)
            detail = str(exc)
            if isinstance(exc, FilmOptimizationRunStartError):
                self._error = f"Unable to start film optimization worker: {detail}"
            elif isinstance(exc, FilmOptimizationRunPreparationError):
                self._error = f"Unable to prepare film optimization worker: {detail}"
            elif "invalid_job_spec" in detail:
                self._error = (
                    "后台队列声明支持膜系优化，但拒绝了作业描述；未提交任务。"
                    "可能是运行中的队列与当前桌面版本不兼容。"
                    f"队列诊断：{detail}")
            else:
                self._error = f"Unable to submit film optimization to the queue: {detail}"
            self.changed.emit()
            return False
        self._frozen_request = base_request
        self._completion_handoff_done = False
        self._result = None
        self._structure_result = None
        self._inspection_objectives = None
        self._result_terminal_state = ""
        self._result_application_eligible = False
        self._baseline = {}
        self._candidate_saved = False
        self._candidate_applied = False
        self._error = ""
        self._progress = 0.0
        self._progress_iteration = self._progress_evaluations = 0
        self._progress_best_error = None
        self._progress_text = "正在启动优化"
        self.changed.emit()
        return True

    @Slot(result=bool)
    def startOptimizationFromShortcut(self) -> bool:
        """Start from the latest owning design when invoked by F6 or routing."""
        if self._pending_reuse_source is not None:
            self._pending_reuse_start = True
            return True
        if self._busy or self._source_closed or self._shutting_down:
            return False
        if self._pending_edit_error:
            if not self._error:
                self._error = "请修正未提交的数字输入后再开始优化。"
            self.changed.emit()
            return False
        if self._source_stale:
            if self._detached_source:
                self._error = "仿真快照已变化，请从仿真窗口重新开始优化。"
                self.changed.emit()
                return False
            self.refreshDesign()
        return self.startOptimization()

    @Slot()
    def cancelOptimization(self) -> None:
        if self._busy and self._cancel_event is not None:
            error = self._run_coordinator.request_cancel()
            if error is not None:
                detail = str(getattr(self._cancel_event, "cancel_error_message", "")
                             or f"{type(error).__name__}: {error}")
                self._error = f"Unable to request queue cancellation: {detail}"
            elif self._error.startswith("Unable to request queue cancellation:"):
                self._error = ""
            self._progress_text = "正在取消；等待当前完整评估结束"
            self.changed.emit()

    def inspectQueueResult(self, job_id: str, payload: dict, *, origin=None) -> bool:
        """Display a verified historical candidate without editing its source.

        The queue factory verifies the row/payload binding before this handoff.
        Frozen plotting objectives are separate from the editable film settings.
        Historical candidates can be saved as a new managed film, never applied.
        """
        try:
            if self.project_context is None or self._project_compute is None:
                raise ValueError("Project queue inspection requires an explicit project")
            if (not isinstance(origin, dict)
                    or origin.get("project_id") != self.project_context.binding.project_id
                    or origin.get("project_root") != str(self.project_context.binding.root)):
                raise ValueError("Queue candidate belongs to a different project")
            if self._busy or self._pending_edit_error or self._shutting_down:
                raise ValueError("请先完成当前优化任务或修正未提交输入，再查看队列候选。")
            if self._result is not None and not self._candidate_saved and not self._candidate_applied:
                raise ValueError("请先保存或清除当前候选，再查看另一队列结果。")
            operation = self._project_compute.capture("inspect-" + str(job_id))
            candidate = _film_result_from_queue(payload)
            structure: Any = candidate if hasattr(candidate, "base_result") else None
            request = (structure.frozen_request.base_request if structure is not None
                       else deserialize_request(payload["request"]))
            result = structure.base_result if structure is not None else candidate
            objectives = []
            for index, objective in enumerate(request.objectives):
                row = _objective_default(index)
                row.update(wavelength_min_nm=objective.wavelength_min_nm,
                           wavelength_max_nm=objective.wavelength_max_nm,
                           wavelength_samples=objective.wavelength_samples,
                           angle_sampling_mode="points",
                           angles_text=", ".join(str(value) for value in objective.angles_deg),
                           polarization=objective.polarization, metric=objective.metric,
                           mode=objective.mode,
                           target_value_percent=(objective.target_value or 0.0) * 100,
                           lower_bound_percent=(objective.lower_bound or 0.0) * 100,
                           upper_bound_percent=(objective.upper_bound * 100
                                                if objective.upper_bound is not None else 100.0))
                row = _resolve_angle_sampling(row)
                objectives.append(row)
        except Exception as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self._project_operation = operation
        self._queue_job_id = str(job_id)
        self._frozen_request = request
        self._result = result
        self._structure_result = structure
        self._inspection_objectives = objectives
        self._result_application_eligible = False
        self._operation_edit_epoch = None
        self._baseline = {}
        self._candidate_saved = self._candidate_applied = False
        self._source_stale = True
        self._plot_objective = 0
        self._plot_angle = float(request.objectives[0].angles_deg[0])
        self._progress = 1.0
        self._result_terminal_state = "队列历史结果"
        self._progress_text = "历史候选仅供查看或另存；未修改当前设计和优化配置。"
        self._error = ""
        self.changed.emit()
        self.plotDataChanged.emit()
        return True

    @Slot(int, object)
    def _on_progress_for_operation(self, operation_id: int,
                                   progress: FilmOptimizationProgress) -> None:
        if (operation_id != self._active_operation_id or not self._busy
                or not self._project_current()):
            return
        if not isinstance(progress, FilmOptimizationProgress):
            return
        cancel_error = str(getattr(self._cancel_event, "cancel_error_message", "") or "")
        if cancel_error:
            self._error = f"Unable to request queue cancellation: {cancel_error}"
        self._progress = float(progress.fraction)
        self._progress_iteration = int(progress.iteration)
        self._progress_evaluations = int(progress.evaluations)
        self._progress_best_error = progress.best_max_normalized_error
        self._progress_text = str(progress.message or progress.state)
        self.changed.emit()

    @Slot(int, str)
    def _on_job_id_for_operation(self, operation_id: int, job_id: str) -> None:
        if operation_id != self._operation_serial or not self._project_current():
            return
        self._queue_job_id = str(job_id)
        if self._project_compute is not None and self._project_operation is not None and job_id:
            admitted = bool(getattr(self._worker, "admitted", False))
            self._project_compute.update_job(
                job_id, "queued" if admitted else "submission_unknown", service_owned=admitted)
        self.changed.emit()

    def _on_angle_preparation_finished(self, operation_id: int, result,
                                       error) -> None:
        pending = self._pending_angle_preparation
        if pending is None or pending.get("operation_id") != operation_id:
            return
        self._pending_angle_preparation = None
        close_requested = self._run_coordinator.take_pending_close()
        if self._shutting_down or operation_id != self._operation_serial:
            return
        cancel_requested = self._run_coordinator.cancel_requested
        if error or cancel_requested:
            self._project_operation = None
            self._prepared_angle_job_id = None
            if cancel_requested or (isinstance(error, dict)
                                    and error.get("state") == "cancelled"):
                self._error = "角度网格准备已取消；队列作业尚未提交。"
                self._progress_text = self._error
            else:
                self._error = f"角度网格准备失败；队列作业尚未提交：{error}"
                self._progress_text = "角度网格准备失败；原设计未改变"
            self.changed.emit()
            if close_requested:
                self.requestClose()
            return
        if not isinstance(result, _PreparedAngleGrid):
            self._project_operation = None
            self._prepared_angle_job_id = None
            self._error = "角度网格准备没有返回有效结果；队列作业尚未提交。"
            self._progress_text = "角度网格准备失败；原设计未改变"
            self.changed.emit()
            if close_requested:
                self.requestClose()
            return

        expected_revision = int(pending["design_revision"])
        expected_epoch = pending.get("edit_epoch")
        current_revision = self._source_revision
        if self.session is not None:
            try:
                current_revision = int(self.session.snapshot().design_revision)
            except (AttributeError, TypeError, ValueError, RuntimeError):
                current_revision = -1
        source_changed = (
            self._source_closed or self._source_stale
            or current_revision != expected_revision
            or self._source_edit_epoch() != expected_epoch
        )
        project_changed = (self._project_compute is not None and (
            bool(getattr(self._project_compute, "editing_frozen", False))
            or not self._project_current()))
        if source_changed or project_changed:
            self._project_operation = None
            self._prepared_angle_job_id = None
            self._source_stale = True
            self._error = ("项目已关闭或正在关闭；未提交队列作业。"
                           if project_changed else
                           "原设计在准备角度网格期间发生编辑；未提交队列作业。")
            self._progress_text = self._error
            self.changed.emit()
            if close_requested:
                self.requestClose()
            return
        if close_requested:
            self._project_operation = None
            self._progress_text = "角度网格已准备；按关闭请求未提交队列作业。"
            self.changed.emit()
            self.requestClose()
            return
        self._prepared_angle_job_id = str(pending["job_id"])
        self._start_optimization(prepared_start=result)

    @Slot(int, object, object, object)
    def _on_finished(self, operation_id: int, result, baseline_rows, error) -> None:
        pending = self._pending_angle_preparation
        if pending is not None and pending.get("operation_id") == operation_id:
            self._on_angle_preparation_finished(operation_id, result, error)
            return
        if self._project_compute is not None and operation_id == self._operation_serial:
            state = ("cancelled" if isinstance(error, dict) and error.get("state") == "cancelled"
                     else "failed" if error else "succeeded")
            self._project_compute.update_job(self._queue_job_id, state)
            if self._project_compute.editing_frozen and not self._shutting_down:
                self._pending_project_completion = (operation_id, result, baseline_rows, error)
                return
        if (self._shutting_down or operation_id != self._operation_serial
                or not self._project_current()):
            return
        structure_result = (result if hasattr(result, "base_result")
                            and hasattr(result, "topology_audit") else None)
        if structure_result is not None:
            result = structure_result.base_result
        self._structure_result = structure_result
        has_candidate = isinstance(result, FilmOptimizationResult)
        self._result_application_eligible = bool(
            not error and has_candidate and result.state == "completed"
            and (structure_result is None or (
                getattr(structure_result, "is_final", False)
                and result.validation_complete)))
        if has_candidate:
            self._result = result
            self._progress_iteration = int(result.iterations)
            self._progress_evaluations = int(result.evaluations)
            self._progress_best_error = result.max_normalized_error
            self._baseline = {
                (int(row["objective_index"]), float(row["angle_deg"])): row
                for row in (baseline_rows or [])
            }
            if self.session is not None:
                snapshot = self.session.snapshot()
                self._source_stale = (self._source_closed
                                      or snapshot.design_revision != result.design_revision)
            else:
                self._source_stale = False
        if isinstance(error, dict) and error.get("state") == "submission_unknown":
            job_id = str(error.get("job_id") or self._queue_job_id)
            self._result_terminal_state = "提交状态未知"
            self._error = (
                "远程提交状态仍未知；不会自动重复提交。"
                f"请使用作业 ID {job_id} 核对状态。"
            )
            self._progress_text = self._error
        elif isinstance(error, dict) and error.get("state") == "submission_failed":
            self._result_terminal_state = "失败"
            self._queue_job_id = ""
            self._error = str(error.get("message") or "队列能力核对或作业提交失败；未创建任务。")
            self._progress_text = "远程作业未接纳；原设计未改变"
        elif isinstance(error, dict) and error.get("state") == "cancelled":
            self._result_terminal_state = "已取消"
            self._error = ""
            if has_candidate:
                self._progress_text = "优化已取消；已保留队列验证的最佳候选供查看或另存，未自动回写。"
            else:
                self._progress_text = str(error.get("message") or "Cancelled")
        elif error:
            self._result_terminal_state = "失败"
            self._error = str(error)
            if has_candidate:
                self._progress_text = "优化失败；已保留队列验证的最佳候选供查看或另存，未自动回写。"
            else:
                self._progress_text = "优化失败；原设计未改变"
        elif isinstance(result, FilmOptimizationResult):
            self._result_terminal_state = (
                "已取消" if result.state == "cancelled" else
                "失败" if result.state == "failed" else
                "已完成" if result.state == "completed" else str(result.state))
            self._progress = 1.0
            self._progress_text = result.message
        else:
            self._error = "优化任务没有返回有效候选；原设计未改变"
        self.changed.emit()
        self.plotDataChanged.emit()
        if (not error and isinstance(result, FilmOptimizationResult)
                and result.state == "completed"):
            self._apply_completed_result(operation_id, result)
        if self._run_coordinator.take_pending_close():
            self.requestClose()

    def _apply_completed_result(self, operation_id: int,
                                result: FilmOptimizationResult) -> bool:
        if self._shutting_down or not self._project_current():
            return False
        if not self._result_application_eligible:
            return False
        source = self._source_controller
        if self._detached_source or source is None:
            self._progress_text = "优化完成；独立快照候选已保留，未回写来源设计。"
            self.changed.emit()
            return False
        if self._cancel_requested:
            self._progress_text = "优化完成但已请求取消；候选未自动回写。"
            self.changed.emit()
            return False
        if self._pending_close or self._source_closed:
            self._error = "来源设计窗口已关闭；优化候选未自动回写。"
            self.changed.emit()
            return False
        if operation_id != self._operation_serial or self.session is None:
            return False
        session = self.session
        if getattr(source, "session", None) is not self.session:
            self._error = "来源设计会话已替换；优化候选未自动回写。"
            self.changed.emit()
            return False
        snapshot = session.snapshot()
        if (snapshot.design_revision != result.design_revision
                or self._source_edit_epoch() != self._operation_edit_epoch):
            self._source_stale = True
            self._error = "原设计在优化期间发生编辑（包括尚未提交的字段）；候选未自动覆盖。"
            self.changed.emit()
            return False
        apply = getattr(source, "applyOptimizedDesign", None)
        if not callable(apply):
            self._error = "来源设计窗口不支持安全回写；优化候选已保留。"
            self.changed.emit()
            return False
        try:
            accepted = bool(apply(deepcopy(dict(result.best_design)),
                                  int(result.design_revision),
                                  int(self._operation_edit_epoch or 0)))
        except Exception as exc:
            self._error = f"自动回写失败：{type(exc).__name__}: {exc}"
            self.changed.emit()
            return False
        if not accepted:
            self._source_stale = True
            self._error = "原设计或未提交编辑已改变；候选未自动覆盖。"
            self.changed.emit()
            return False
        committed = session.snapshot()
        self._source_revision = int(committed.design_revision)
        self._design = deepcopy(dict(committed.design))
        self._path = str(committed.path or "")
        self._source_stale = False
        self._candidate_applied = True
        self._candidate_saved = True
        self._error = ""
        if result.targets_met:
            self._progress_text = str(result.message or "优化完成") + " · 已自动回写原设计。"
        else:
            self._progress_text = (str(result.message or "优化完成")
                                   + " · 采样目标未全部满足；候选已回写，请核对结果。")
        self.changed.emit()
        self.plotDataChanged.emit()
        return True

    def _open_completed_candidate_preview(self, result: FilmOptimizationResult) -> None:
        if self._completion_handoff_done:
            return
        self._completion_handoff_done = True
        launcher = self.parent()
        opener = getattr(launcher, "open_window", None)
        if not callable(opener) or self._frozen_request is None:
            return
        try:
            payload = create_optimization_preview_handoff(self._frozen_request, result)
            instance_id = opener("film_simulation", mode="new", payload=payload)
            if not instance_id:
                self._error = "Optimization finished, but the independent preview window could not be opened."
                self.changed.emit()
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            self._error = f"Optimization finished, but its preview handoff failed: {exc}"
            self.changed.emit()

    @Slot(result=bool)
    def applyCandidate(self) -> bool:
        if self.session is None:
            self._error = "仿真快照没有可应用的编辑会话；请另存优化候选。"
            self.changed.emit()
            return False
        if not self.canApply or self._result is None:
            self._error = "候选已过期、正在计算或不存在；没有修改原设计。"
            self.changed.emit()
            return False
        if not self._result.targets_met:
            self.applyConfirmationRequested.emit(
                "当前候选未达到全部离散采样目标。仍要把这个未达标候选应用到当前设计吗？")
            return False
        return self._apply_candidate()

    @Slot(result=bool)
    def confirmApplyCandidate(self) -> bool:
        if self.session is None:
            self._error = "仿真快照没有可应用的编辑会话；请另存优化候选。"
            self.changed.emit()
            return False
        if not self.canApply:
            self._error = "候选已过期、正在计算或不存在；没有修改原设计。"
            self.changed.emit()
            return False
        return self._apply_candidate()

    def _apply_candidate(self) -> bool:
        if not self._project_current():
            self._error = "来源项目已关闭或重新打开；候选没有回写。"
            self.changed.emit()
            return False
        if self.session is None:
            self._error = "仿真快照没有可应用的编辑会话；请另存优化候选。"
            self.changed.emit()
            return False
        session = self.session
        result = self._result
        if result is None:
            self._error = "没有可应用的候选；原设计未改变。"
            self.changed.emit()
            return False
        if not self._result_application_eligible:
            self._error = "取消或失败任务的最佳候选只可查看或另存，不能应用。"
            self.changed.emit()
            return False
        if self._source_closed or self._source_controller is None:
            self._error = "来源设计窗口已关闭；没有修改原设计。"
            self.changed.emit()
            return False
        # The design controller checks both the committed revision and any
        # text still being edited before delegating the atomic session update.
        apply = getattr(self._source_controller, "applyOptimizedDesign", None)
        if not callable(apply):
            self._error = "来源设计窗口不支持安全候选应用；原设计未改变。"
            self.changed.emit()
            return False
        edit_epoch = self._operation_edit_epoch
        if edit_epoch is None:
            self._error = "候选缺少来源编辑版本；原设计未改变。"
            self.changed.emit()
            return False
        try:
            accepted = bool(apply(deepcopy(dict(result.best_design)),
                                  int(result.design_revision), int(edit_epoch)))
        except Exception as exc:
            self._error = f"应用候选失败：{type(exc).__name__}: {exc}"
            self.changed.emit()
            return False
        if not accepted:
            self._error = "原设计版本已变化或候选无效；整个结果均未应用。"
            self._source_stale = True
            self.changed.emit()
            return False
        snapshot = session.snapshot()
        self._source_revision = int(snapshot.design_revision)
        self._design = deepcopy(snapshot.design)
        self._path = str(snapshot.path or "")
        self._source_stale = False
        self._candidate_applied = True
        self._candidate_saved = True
        self._error = ""
        self.changed.emit()
        return True

    @Slot(QUrl, bool, result=bool)
    def saveCandidate(self, url: QUrl, overwrite: bool = False) -> bool:
        path = url.toLocalFile() if isinstance(url, QUrl) else str(url)
        return self.saveCandidatePath(path, overwrite)

    @Slot(result=bool)
    def saveCandidateToProject(self) -> bool:
        if self.project_context is None or self._result is None or self._busy:
            return False
        try:
            if not self._project_current():
                raise ValueError("来源项目已关闭或重新打开")
            design = deepcopy(dict(self._result.best_design))
            project_service = self.project_context.service
            frozen = (self._frozen_request.frozen_material_snapshot
                      if self._frozen_request is not None else None)
            rows = frozen["materials"] if frozen is not None else project_service.materials._records()
            materials = {row["id"]: row for row in rows}
            saved = project_service.create_film(design, materials=materials)
        except Exception as exc:
            self._error = f"保存项目候选失败：{exc}"
            self.changed.emit()
            return False
        self._candidate_saved = True
        self.project_context.editor.refresh_saved_designs()
        self._error = ""
        self._progress_text = f"候选已保存到项目：{saved.design['name']}"
        self.changed.emit()
        return True

    @Slot(str, bool, result=bool)
    def saveCandidatePath(self, path: str, overwrite: bool = False) -> bool:
        if self.project_context is not None:
            return self.saveCandidateToProject()
        if self._result is None or self._busy:
            self._error = "当前没有可另存的优化候选。"
            self.changed.emit()
            return False
        path = str(Path(path).expanduser())
        if not path.lower().endswith(".cfilm"):
            path += ".cfilm"
        if Path(path).exists() and not overwrite:
            self.overwriteRequested.emit(path)
            return False
        try:
            save = getattr(self.service, "save", None)
            if not callable(save):
                raise ValueError("当前膜系服务不支持另存候选")
            save(path, deepcopy(dict(self._result.best_design)), overwrite=bool(overwrite))
        except Exception as exc:
            self._error = f"另存候选失败：{type(exc).__name__}: {exc}"
            self.changed.emit()
            return False
        self._candidate_saved = True
        self._error = ""
        self._progress_text = f"候选已另存：{path}"
        self.changed.emit()
        return True

    @Slot()
    def requestClose(self) -> None:
        if self.project_context is not None and self._busy:
            if self._pending_angle_preparation is not None:
                # A local pre-submission preparation has no project-owned job
                # to preserve; cancel it before disposing this observer.
                self._run_coordinator.request_cancel()
            self.detach_observers()
            self._run_coordinator.detach_service_observer()
            self.closeConfirmed.emit()
            return
        if self._busy:
            error = self._run_coordinator.request_close()
            if error is not None:
                self._error = f"Unable to request queue cancellation: {error}"
            elif self._error.startswith("Unable to request queue cancellation:"):
                self._error = ""
            self._progress_text = "正在取消；等待当前完整评估结束"
            self.changed.emit()
            return
        if self._result is not None and not self._candidate_saved and not self._candidate_applied:
            self.closeConfirmationRequested.emit()
        else:
            self.closeConfirmed.emit()

    @Slot(bool)
    def resolveClose(self, discard_candidate: bool) -> None:
        if not discard_candidate:
            return
        self._result = None
        self._result_terminal_state = ""
        self._result_application_eligible = False
        self._baseline = {}
        self.closeConfirmed.emit()

    @Slot(int, result=bool)
    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """Cancel and wait for this window's private pool during test/teardown."""
        self.detach_observers()
        self._pending_project_completion = None
        if self._project_compute is not None:
            self._project_compute.unregister_thaw_callback(self._on_project_unfrozen)
        return self._run_coordinator.shutdown(timeout_ms)

