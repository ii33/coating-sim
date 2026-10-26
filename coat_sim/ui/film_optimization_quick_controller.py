"""Qt adapter for the independent thin-film thickness optimizer window."""
from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
import inspect
import json
from importlib import import_module
import math
from pathlib import Path
import re
import threading
import time
from uuid import uuid4
from typing import TYPE_CHECKING, Any, Literal, Mapping, Protocol, cast
import weakref
from coat_sim.project_session import OperationBinding

from PySide6.QtCore import Property, QObject, QRunnable, QThreadPool, Qt, QTimer, QUrl, Signal, Slot

from coat_sim.assistant_diagnostics import trace
from coat_sim.ui.film_candidate_display_controller import FilmCandidateDisplayController
from coat_sim.ui.film_display_format import format_film_thickness

from coat_sim.core.film_optimization import (
    FilmConditionError,
    FilmOptimizationObjective,
    FilmOptimizationProgress,
    FilmOptimizationRequest,
    FilmOptimizationResult,
    FilmThicknessVariable,
    freeze_film_optimization_request,
)
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint, FilmObjectiveCoverage, validate_objective_criteria
from coat_sim.core.film_optimization_preview import create_optimization_preview_handoff
from coat_sim.core.film_optimization_job import CANDIDATE_KIND, RESULT_KIND, CONTRACT_VERSION, deserialize_request
from coat_sim.scheduler import SchedulerClient, freeze_spec
from coat_sim.scheduler.desktop import (
    RemoteCancelEvent,
    observe_job,
    resolve_unknown_submission,
)
from coat_sim.ui.film_synthesis_quick_adapter import (
    SynthesisQuickResult, build_synthesis_request,
    synthesis_base_request, synthesis_cpu_workers, synthesis_options, synthesis_result_view, synthesis_spec,
    synthesis_structure_search_seed,
    synthesis_preview_handoff,
    verify_synthesis_file_references,
)
from coat_sim.ui.film_synthesis_forward_probe import FilmSynthesisForwardProbe
from coat_sim.ui.film_sensitivity_quick_controller import FilmSensitivityQuickController
from coat_sim.ui.film_objective_progress_editor import ObjectiveProgressDraft, PROGRESS_FIELD_LABELS
from coat_sim.ui.film_optimization_run_coordinator import (
    FilmOptimizationRunCoordinator,
    FilmOptimizationRunPreparationError,
    FilmOptimizationRunStartError,
)

if TYPE_CHECKING:
    from coat_sim.core.film_optimization_structure import FilmStructureOptimizationResult
    from coat_sim.core.film_reduction import FilmReductionResult
    from coat_sim.ui.film_manufacturing_quick_controller import FilmManufacturingQuickController


class _SynthesisMaterialService(Protocol):
    """Shared film-purpose picker contract of standalone and project services."""

    def material_options(self, purpose: str = "film") -> list[dict[str, Any]]: ...


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


def _edited_thickness_bound(value, label: str) -> float:
    number = _number(value, label)
    try:
        result = float(Decimal(str(number)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))
    except (ArithmeticError, OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be nonnegative and finite") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{label} must be nonnegative and finite")
    return result


def _constraint_editor_default(enabled: bool, upper: float = 100.0) -> dict:
    return {"enabled": enabled, "mode": "range", "target_value_percent": None,
            "lower_bound_percent": 0.0, "upper_bound_percent": upper,
            "strict_lower": False, "strict_upper": False}


def _constraint_editor_from_request(constraint: FilmObjectiveConstraint) -> dict:
    return {"enabled": constraint.enabled, "mode": constraint.mode,
            "target_value_percent": (None if constraint.target_value is None
                                     else constraint.target_value * 100),
            "lower_bound_percent": ((constraint.target_value or 0.0) * 100
                                    if constraint.mode == "value" else
                                    (constraint.lower_bound or 0.0) * 100),
            "upper_bound_percent": ((constraint.target_value or 0.0) * 100
                                    if constraint.mode == "value" else
                                    (1.0 if constraint.upper_bound is None else constraint.upper_bound) * 100),
            "strict_lower": constraint.strict_lower, "strict_upper": constraint.strict_upper}


def _constraint_from_editor(row: dict) -> FilmObjectiveConstraint:
    # Disabled drafts remain editor state and never enter the frozen calculation.
    if not row["enabled"]:
        return FilmObjectiveConstraint(enabled=False)
    lower = _number(row["lower_bound_percent"], "目标下限%") / 100
    upper = _number(row["upper_bound_percent"], "目标上限%") / 100
    mode: Literal["value", "range"] = (
        "value" if lower == upper and not (row["strict_lower"] or row["strict_upper"]) else "range")
    return FilmObjectiveConstraint(enabled=True, mode=mode,
        target_value=lower if mode == "value" else None,
        lower_bound=lower if mode == "range" else None,
        upper_bound=upper if mode == "range" else None,
        strict_lower=row["strict_lower"], strict_upper=row["strict_upper"])


def _validate_editor_criteria(row: dict) -> None:
    validate_objective_criteria({"pointwise": _constraint_from_editor(row["pointwise"]),
        "mean": _constraint_from_editor(row["mean"]),
        "weight": row["weight"], "normalization_scale": 1.0})


_CRITERION_BOUNDS_FIELDS = frozenset(
    f"{group}.{bound}_bound_percent" for group in ("mean", "pointwise")
    for bound in ("lower", "upper"))
_OBJECTIVE_BOUNDS_FIELDS = _CRITERION_BOUNDS_FIELDS | {"wavelength_min_nm", "wavelength_max_nm"}


def _objective_default(index: int) -> dict:
    return {
        "id": uuid4().hex,
        "name": f"目标 {index + 1}",
        "wavelength_min_nm": 400.0,
        "wavelength_max_nm": 700.0,
        "wavelength_samples": 301,
        "sampling_basis": "step",
        "wavelength_step_nm": 1.0,
        "actual_wavelength_step_nm": 1.0,
        "angle_sampling_mode": "range",
        "angle_min_deg": 0.0,
        "angle_max_deg": 0.0,
        "angle_step_deg": 1.0,
        "actual_angle_step_deg": 0.0,
        "angle_sample_count": 1,
        "angles_text": "0",
        "polarization": "unpolarized",
        "metric": "R",
        "pointwise": _constraint_editor_default(True, 5.0),
        "mean": _constraint_editor_default(False, 100.0),
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


class _InitialLoadSignals(QObject):
    finished = Signal(object, object, object)


class _InitialLoadWorker(QRunnable):
    """Read Python-only dependencies; never touch the window or controller."""

    def __init__(self, catalog: object, signals: _InitialLoadSignals) -> None:
        super().__init__()
        self.catalog = catalog
        self.signals = signals

    def run(self) -> None:
        started = time.perf_counter()
        scipy_result = _scipy_runtime_availability()
        trace("film_optimization_scipy_checked", elapsed_s=time.perf_counter() - started,
              state="available" if not scipy_result[0] else "unavailable")
        started = time.perf_counter()
        try:
            reader = getattr(self.catalog, "_records", None)
            if not callable(reader):
                raise ValueError("material catalog does not support enumeration")
            records, material_error = reader(), ""
        except Exception as exc:
            records, material_error = [], f"{type(exc).__name__}: {exc}"
        trace("film_optimization_materials_read", elapsed_s=time.perf_counter() - started,
              state="ready" if not material_error else "failed")
        try:
            self.signals.finished.emit(scipy_result, records, material_error)
        except RuntimeError:
            # The window may have been destroyed while the read was running.
            pass


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
    editable_snapshot: dict | None = None
    display_score_config: dict | None = None


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
            structure = self.search_mode in {"structure", "reduction"}
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
            editable_snapshot = None
            if self.search_mode == "reduction":
                from coat_sim.core.film_reduction import freeze_film_reduction_request
                request = freeze_film_reduction_request(self.service, request)
            elif structure:
                from coat_sim.core.film_optimization_structure import (
                    freeze_film_structure_optimization_request,
                )
                request = freeze_film_structure_optimization_request(self.service, request)
            elif self.search_mode == "synthesis":
                from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
                request = freeze_film_synthesis_request(self.service, request, cancel_event=self.cancel_event)
                from coat_sim.core.film_physical_generator import freeze_synthesis_editable_snapshot
                editable_snapshot = freeze_synthesis_editable_snapshot(
                    self.service, request.environment, request.frozen_material_snapshot)
            else:
                request = freeze_film_optimization_request(self.service, request)
            spec = (synthesis_spec(self.service, request) if self.search_mode == "synthesis" else
                    freeze_spec(self.service, "film_optimization", {"request": request}))
            if self.cancel_event.is_set():
                raise InterruptedError("角度准备已取消；未提交队列任务。")
            self.signals.finished.emit(
                self.operation_id, _PreparedAngleGrid(request, spec, editable_snapshot), None, None)
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


def _film_result_from_queue(
    value: Any,
) -> FilmOptimizationResult | FilmStructureOptimizationResult | FilmReductionResult:
    """Decode fixed-topology, structure, or reduction queue result carriers."""
    if isinstance(value, dict) and value.get("kind") == "coat_sim.film_reduction_result":
        from coat_sim.core.film_reduction import film_reduction_result_from_payload
        return film_reduction_result_from_payload(value)
    if (isinstance(value, dict)
            and value.get("kind") == "coat_sim.film_structure_optimization_result"):
        from coat_sim.core.film_optimization_structure import film_structure_result_from_payload

        return film_structure_result_from_payload(value)
    if (not isinstance(value, dict) or value.get("schema_version") != CONTRACT_VERSION
            or value.get("kind") != RESULT_KIND):
        raise ValueError("queue result is not a typed film optimization result")
    request = deserialize_request(value.get("request", {}))
    if not isinstance(request, FilmOptimizationRequest):
        raise ValueError("fixed-topology queue result requires a matching optimization request")
    candidate = value.get("candidate")
    if (not isinstance(candidate, dict) or candidate.get("kind") != CANDIDATE_KIND
            or candidate.get("schema_version") != CONTRACT_VERSION):
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
        solver_acceptable=quality["solver_acceptable"],
        validation_solver_acceptable=solver_validation.get("validation_solver_acceptable"),
        raw_coverage=tuple(FilmObjectiveCoverage(**row) for row in candidate["raw_coverage"]),
        validation_raw_coverage=tuple(FilmObjectiveCoverage(**row)
            for row in solver_validation.get("validation_raw_coverage", [])),
    )


def _cancel_snapshot_from_queue(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    execution = value.get("execution")
    if not isinstance(execution, dict):
        return None
    marker = execution.get("cancel_snapshot")
    if marker is None:
        return None
    if (not isinstance(marker, dict)
            or set(marker) != {"source", "replay_performed", "candidate_available"}
            or marker.get("source") != "worker_completed_reply"
            or marker.get("replay_performed") is not False
            or type(marker.get("candidate_available")) is not bool):
        raise ValueError("queue cancel snapshot marker is invalid")
    return deepcopy(marker)


@dataclass(frozen=True)
class _FilmOptimizationTerminal:
    result: Any
    cancel_snapshot: dict[str, Any] | None = None


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
        self._display_callback: Any = None
        self._display_bind: Any = None
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
        message = str(value.get("message", value.get("status", "")))
        if value.get("actual_backend") == "gpu" and value.get("gpu_runtime") == "xpu":
            message = "Intel XPU 批量前向 · " + str(value.get("dtype","")) + " · " + message
        try:
            progress = FilmOptimizationProgress(
                state=state,
                fraction=max(0.0, min(1.0, float(value.get("fraction", 0.0)))),
                iteration=max(0, int(value.get("iteration", value.get("step_index", 0)))),
                evaluations=max(0, int(value.get("evaluations", 0))),
                best_max_normalized_error=error,
                message=message,
            )
        except (TypeError, ValueError, OverflowError):
            return
        self.signals.progress.emit(self.operation_id, progress)

    def _decode_result(self, payload):
        result = _film_result_from_queue(payload)
        marker = _cancel_snapshot_from_queue(payload)
        if marker is not None or result.state == "cancelled":
            return _FilmOptimizationTerminal(result, marker)
        return result

    def _verified_terminal_candidate(self) -> Any | None:
        """Fetch only a queue-verified checkpoint-backed terminal candidate."""
        if not self.admitted:
            return None

        result_method = getattr(self.store, "result", None)
        if not callable(result_method):
            return None
        try:
            payload = result_method(self.job_id)
            return self._decode_result(payload)
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
            if callable(self._display_bind):
                try:
                    self._display_bind(self.store, self.job_id)
                except Exception:
                    pass  # Optional display binding cannot fail the queue observer.
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

            display_options: dict[str, Any] = {}
            if callable(self._display_callback) and "display" in inspect.signature(observe_job).parameters:
                display_options["display"] = self._display_callback
            payload = observe_job(
                self.store, self.job_id, self._publish_queue_progress,
                disconnected=self._observer_disconnected.is_set, **display_options,
            )
            result = self._decode_result(payload)
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
            from coat_sim.scheduler.remote_client import RemoteSubmissionUnknown
            self._record_state("submission_unknown" if isinstance(exc, RemoteSubmissionUnknown) else "failed")
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
                            "队列提交状态仍未知；不会自动重复提交。"
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
    """Local admission uses the same nonblocking, exact-ID cancellation protocol."""

    def __init__(self, client, job_id: str) -> None:
        super().__init__(client, job_id, pending_admission=True)
        self.cancel_intent = self


class _QueueCompatibilityError(RuntimeError):
    pass


class _FilmSynthesisTask(_FilmOptimizationTask):
    def _decode_result(self, payload):
        return synthesis_result_view(payload)

    def _publish_queue_progress(self, value):
        if isinstance(value, dict):
            value = dict(value)
            value["iteration"] = value.get("cycle", value.get("iteration", 0))
            if value.get("stage") == "audit_incumbent":
                self._audited_layer_count = value.get("layer_count")
            parts = [str(value.get("stage", "synthesis")),
                     str(value.get("message", value.get("status", ""))),
                     f"当前报告层数 {value.get('layer_count', '?')}，已审计最好层数 {getattr(self, '_audited_layer_count', '尚无完整审计')}",
                     f"候选评价 {value.get('evaluations', 0)}"]
            if "heartbeat_sequence" in value:
                parts.append(f"heartbeat {value['heartbeat_sequence']}")
            for key in ("algorithm_progress_age_seconds", "worker_heartbeat_age_seconds",
                        "stalled", "unresponsive", "physical_evaluations", "wavelength_points"):
                if key in value:
                    parts.append(f"{key}={value[key]}")
            value["message"] = " | ".join(parts)
        super()._publish_queue_progress(value)


class FilmOptimizationQuickController(QObject):
    """Per-window optimization state; the edit session remains the design owner."""

    changed = Signal()
    plotDataChanged = Signal()
    synthesisPreviewRequested = Signal()
    overwriteRequested = Signal(str)
    closeConfirmed = Signal()
    closeConfirmationRequested = Signal()
    applyConfirmationRequested = Signal(str)
    sourceReuseConfirmationRequested = Signal()

    _sessionChange = Signal(object)
    _cancelTransportStatus = Signal(str, str)

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
                 project_context=None, defer_expensive_load: bool = False) -> None:
        super().__init__(parent)
        self._open_started_at = time.perf_counter()
        project_context = (project_context if project_context is not None
                           else getattr(source_controller, "project_context", None))
        self._run_coordinator = FilmOptimizationRunCoordinator(
            self, service_owned=project_context is not None)
        self._run_coordinator.progress.connect(self._on_progress_for_operation)
        self._run_coordinator.jobIdChanged.connect(self._on_job_id_for_operation)
        self._run_coordinator.finished.connect(self._on_finished)
        self._cancelTransportStatus.connect(self._on_cancel_transport_status)
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

        self._deferred_batch_enabled = False
        self._film_cpu_workers = 1
        self._film_cpu_axis = "auto"
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
        self._live_display = FilmCandidateDisplayController(self)
        from .theme_service import ThemeService
        ThemeService.instance().filmThicknessDecimalsChanged.connect(self._on_film_display_precision)
        self._sensitivity_controller: FilmSensitivityQuickController | None = None
        self._manufacturing_controller: FilmManufacturingQuickController | None = None
        self._result_terminal_state = ""
        self._result_application_eligible = False
        self._frozen_request: FilmOptimizationRequest | None = None
        self._queue_job_id = ""
        self._continuation_record: dict[str, Any] | None = None
        self._continuation_unavailable_reason = "尚无已取消的完整评分候选。"
        self._prior_run_fallback: dict[str, Any] | None = None
        self._attempt_full_request = None
        self._attempt_source_binding: dict[str, Any] | None = None
        self._attempt_spec_fingerprint = ""
        self._attempt_editable_snapshot: dict[str, Any] | None = None
        self._attempt_operation_id: int | None = None
        self._attempt_job_id = ""
        self._attempt_session = None
        self._attempt_source_controller = None
        self._attempt_source_revision: int | None = None
        self._attempt_source_path: str | None = None
        self._attempt_edit_epoch: int | None = None
        self._attempt_project_operation: OperationBinding | None = None
        self._attempt_origin: dict[str, Any] = {}
        self._attempt_cancel_event = None
        self._attempt_search_mode = ""
        self._attempt_materials_owner: Any = None
        self._attempt_frozen_material_snapshot: dict[str, Any] | None = None
        self._terminal_operation_id: int | None = None
        self._cancel_intent_operation_id: int | None = None
        self._cancel_intent_job_id = ""
        self._cancel_intent_requested = False
        self._cancel_intent_accepted = False
        self._cancel_intent_consumed = False
        self._attempt_cancel_snapshot: dict[str, Any] | None = None
        self._completion_handoff_done = False
        self._baseline: dict[tuple[int, float], dict] = {}
        self._candidate_saved = False
        self._candidate_applied = False
        self._search_mode = "fixed_topology"
        self._reduction_mode = "combined"
        self._reduction_result: FilmReductionResult | None = None
        self._requested_search_mode = getattr(source_controller, "_requested_optimizer_search_mode", "")
        if self._requested_search_mode:
            source_controller._requested_optimizer_search_mode = ""
        self._synthesis_options = synthesis_options({})
        self._synthesis_options.update(randomize_template_search=False, structure_search_seed=None)
        self._synthesis_structure_search_seed_text = ""
        self._synthesis_structure_search_seed_error = ""
        self._synthesis_progress_draft = ObjectiveProgressDraft()
        self._synthesis_progress_error = ""
        self._synthesis_progress_edit_dirty = False
        self._synthesis_actual_structure_search_seed: int | None = None
        self._synthesis_cpu_workers = 0
        self._synthesis_forward_backend = "cpu"
        self._synthesis_forward_device: dict[str, Any] | None = None
        self._synthesis_forward_probe = FilmSynthesisForwardProbe(parent=self)
        self._synthesis_forward_probe.changed.connect(self.changed.emit)
        self._synthesis_forward_probe.finished.connect(self._on_synthesis_forward_probe_finished)
        self._synthesis_use_current_seed = False
        self._synthesis_assume_unknown_k_zero = False
        self._synthesis_material_rows: list[dict[str, str]] = []
        self._synthesis_rows_initialized = False
        self._synthesis_material_catalog_revision = 0
        self._synthesis_payload: dict[str, Any] | None = None
        self._synthesis_editable_snapshot: dict[str, Any] | None = None
        self._structure_mode = "combined"
        self._structure_material_options: list[dict] = []
        self._structure_material_error = ""
        self._initial_loading = bool(defer_expensive_load)
        self._initial_load_started = False
        self._initial_load_closed = False
        self._initial_load_failed = False
        self._pending_shortcut_start = False
        self._initial_load_signals: _InitialLoadSignals | None = None
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
        self._angle_draft: dict[str, str] | None = None
        if self._initial_loading:
            self._scipy_import_error, self._trf_callback_available = "", False
        else:
            self._scipy_import_error, self._trf_callback_available = (
                _scipy_runtime_availability())
        self._objectives = [_objective_default(0)]
        self._objective_range_drafts: dict[tuple[str, str], str] = {}
        self._objective_range_errors: dict[tuple[str, str], str] = {}
        self._objective_range_display: dict[tuple[str, str], str] = {}
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
        self._configuration_defaults = self.exportOptimizationConfiguration()
        if not self._initial_loading:
            self._configuration_loading = False
            self._restore_session_configuration(reset_missing=False)

        self._attach_source_observers()
        if not self._initial_loading and self._requested_search_mode:
            self.requestSearchMode(self._requested_search_mode)
        self._compute_target.changed.connect(self.changed.emit)
        self._sync_film_execution_target()
        if self._initial_loading:
            trace("film_optimization_controller_constructed",
                  elapsed_s=time.perf_counter() - self._open_started_at)

    @Property(bool, notify=changed)
    def loading(self) -> bool:
        return self._initial_loading

    @Slot()
    def startLoading(self) -> None:
        """Called once after the visible window exists; never waits for the worker."""
        if not self._initial_loading or self._initial_load_started or self._initial_load_closed:
            return
        self._initial_load_started = True
        trace("film_optimization_window_show_called",
              elapsed_s=time.perf_counter() - self._open_started_at)
        signals = _InitialLoadSignals(self)
        signals.finished.connect(self._finish_initial_load, Qt.ConnectionType.QueuedConnection)
        self._initial_load_signals = signals
        catalog = getattr(self.service, "materials", None)
        QThreadPool.globalInstance().start(_InitialLoadWorker(catalog, signals))

    @Slot(object, object, object)
    def _finish_initial_load(self, scipy_result: tuple[str, bool],
                             records: list[dict], material_error: str) -> None:
        if not self._initial_loading or self._initial_load_closed:
            return
        self._scipy_import_error, self._trf_callback_available = scipy_result
        try:
            # Source edits may have arrived while the worker ran. The usual
            # unchanged case keeps the initial schematic and avoids a second
            # material-backed source read on the GUI thread.
            if not self._source_closed and self.session is not None:
                snapshot = self.session.snapshot()
                if (snapshot.design_revision != self._source_revision
                        or snapshot.design.get("id") != self._design.get("id")
                        or str(snapshot.path or "") != self._path):
                    self._load_source(preserve_variables=False)
            if material_error:
                self._structure_material_options = []
                self._structure_material_error = material_error
            else:
                self._refresh_structure_material_options(
                    preserve_selection=False, records=records)
            self._configuration_loading = False
            if not self._source_closed:
                self._restore_session_configuration(reset_missing=False)
        except Exception as exc:
            self._initial_load_failed = True
            self._error = f"优化配置加载失败：{type(exc).__name__}: {exc}"
            trace("film_optimization_initial_load_failed", error=exc)
        finally:
            self._initial_loading = False
            self._configuration_loading = False
            self._initial_load_signals = None
            self.changed.emit()
            trace("film_optimization_initial_load_finished",
                  elapsed_s=time.perf_counter() - self._open_started_at,
                  state="failed" if self._initial_load_failed or material_error else "ready")
        if self._requested_search_mode:
            self.requestSearchMode(self._requested_search_mode)
        pending_shortcut = self._pending_shortcut_start
        self._pending_shortcut_start = False
        if pending_shortcut and not material_error and not self._error and not self._source_closed:
            QTimer.singleShot(0, self.startOptimizationFromShortcut)

    def _batch_execution_enabled(self) -> bool:
        return bool(self._deferred_batch_enabled and self._solver_id == "differential_evolution"
                    and self._search_mode == "fixed_topology")

    def _selected_execution_binding(self):
        binding = self._compute_target.execution_binding()
        if binding["kind"] == "film_cpu_v1":
            binding.update(cpu_workers=self._film_cpu_workers, cpu_parallel_axis=self._film_cpu_axis)
        return binding

    @Property(int, constant=True)
    def filmCpuWorkerLimit(self) -> int:
        from coat_sim.core.film_performance import available_cpu_workers
        return min(1024, available_cpu_workers())

    @Property(int, notify=changed)
    def filmCpuWorkers(self) -> int:
        return self._film_cpu_workers

    @Slot(int, result=bool)
    def setFilmCpuWorkers(self, value: int) -> bool:
        if self._busy or self._shutting_down or not 1 <= value <= cast(int, self.filmCpuWorkerLimit):
            return False
        self._film_cpu_workers = value
        self._invalidate_result()
        return True

    @Property(str, notify=changed)
    def filmCpuAxis(self) -> str:
        return self._film_cpu_axis

    @Slot(str, result=bool)
    def setFilmCpuAxis(self, value: str) -> bool:
        if self._busy or self._shutting_down or value not in {"auto", "candidates", "wavelengths"}:
            return False
        self._film_cpu_axis = value
        self._invalidate_result()
        return True

    def _film_xpu_unavailable_reason(self) -> str:
        if getattr(self,"_search_mode",None) != "fixed_topology" or getattr(self,"_solver_id",None) != "differential_evolution":
            return "Intel XPU 仅支持固定拓扑差分进化；其他方法请选 CPU。"
        if not getattr(self,"_deferred_batch_enabled",False):
            return "请显式勾选延迟批量评估；其轨迹不同于旧串行模式。"
        objectives = getattr(self,"_objectives",[])
        try:
            if len(objectives) != 1 or _angle_sample_count(objectives[0]) != 1:
                return "Intel XPU v1 仅支持一个目标条件和一个固定角度。"
        except (ValueError,TypeError,KeyError):
            return "请先修正目标角度配置。"
        design = getattr(self,"_design",{})
        if (design.get("first_surface","front")!="front"
                or design.get("substrate",{}).get("sides")!="single" or design.get("back_layers")):
            return "Intel XPU v1 仅支持前表面、单面半无限基片；该设计请选 CPU。"
        if any(row.get("enabled") and row.get("side")!="front" for row in getattr(self,"_variables",[])):
            return "Intel XPU v1 仅支持前表面厚度变量。"
        return ""

    def _sync_film_execution_target(self) -> None:
        if not hasattr(self,"_compute_target") or not hasattr(self,"_solver_id"):
            return
        self._compute_target.configure_film_context(self._film_xpu_unavailable_reason(), self._batch_execution_enabled())

    @Property(bool, notify=changed)
    def deferredBatchEnabled(self) -> bool:
        return self._deferred_batch_enabled

    @Slot(bool, result=bool)
    def setDeferredBatchEnabled(self, enabled: bool) -> bool:
        if self._busy or self._shutting_down:
            return False
        if enabled and (self._solver_id!="differential_evolution" or self._search_mode!="fixed_topology"):
            self._error = "请先选择固定拓扑差分进化。"
            self.changed.emit()
            return False
        self._deferred_batch_enabled = bool(enabled)
        self._invalidate_result()
        return True

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
            self._request_optimization_cancel(user_initiated=False)

    def _on_project_unfrozen(self) -> None:
        pending, self._pending_project_completion = self._pending_project_completion, None
        if pending is not None and not self._shutting_down:
            self._on_finished(*pending)

    def _project_current(self) -> bool:
        return (self._project_compute is None or
                (self._project_operation is not None
                 and self._project_compute.accepts(self._project_operation)))

    @Property(QObject, constant=True)
    def liveDisplay(self):
        return self._live_display

    @Property(QObject, constant=True)
    def computeTarget(self):
        return self._compute_target

    @Property(QObject, constant=True)
    def sensitivityController(self):
        if self._sensitivity_controller is None:
            self._sensitivity_controller = FilmSensitivityQuickController(self)
            self._sensitivity_controller.changed.connect(self.changed.emit)
        return self._sensitivity_controller

    @Property(QObject, constant=True)
    def manufacturingController(self):
        if self._manufacturing_controller is None:
            from coat_sim.ui.film_manufacturing_quick_controller import FilmManufacturingQuickController
            self._manufacturing_controller = FilmManufacturingQuickController(self)
            self._manufacturing_controller.changed.connect(self.changed.emit)
        return self._manufacturing_controller

    def _manufacturing_can_apply(self, payload: Mapping[str, Any],
                                 context: Mapping[str, Any] | None, snapshot: Any, *,
                                 full_validation: bool = False) -> bool:
        """Fence a worker-validated, explicitly selected route before source mutation."""
        panel = self._manufacturing_controller
        session = self.session
        if session is None or context is None:
            return False
        if (panel is None or id(panel._payload) != id(payload) or id(panel._context) != id(context)
                or panel.busy or self._busy or self._shutting_down
                or (self._sensitivity_controller is not None and self._sensitivity_controller.busy)
                or self._source_closed or self._observers_detached
                or self._source_controller is None
                or id(context.get("session")) != id(session)
                or context.get("source") is not self._source_controller
                or payload.get("state") != "completed" or payload.get("is_final") is not True
                or not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("audit_score"), Mapping)
                or type(snapshot.get("training_score", {}).get("nominal_pass")) is not bool
                or type(snapshot["audit_score"].get("nominal_pass")) is not bool):
            return False
        if not any(route.get("champion") is snapshot for route in payload.get("result", {}).get("routes", ())):
            return False
        compute, operation = context.get("project_compute"), context.get("project_operation")
        if compute is not self._project_compute:
            return False
        if compute is not None and (operation is None or compute.editing_frozen or not compute.accepts(operation)):
            return False
        if (session.snapshot().design_revision != context.get("revision")
                or self._source_edit_epoch() != context.get("epoch")
                or str(Path(getattr(self.service, "root", Path.cwd())).resolve()) != context.get("project_root")):
            return False
        synthesis = context.get("synthesis_request")
        frozen = context.get("frozen_request")
        if (synthesis is None or not isinstance(frozen, Mapping)
                or not isinstance(context.get("editable_snapshot"), Mapping)):
            return False
        # QML readiness is a cheap identity/version gate. Full request expansion,
        # content hashing and live catalog reads belong to the explicit action,
        # not repeated property reads while the dialog renders or animates.
        if not full_validation:
            return True
        current = self._build_request(require_variables=False, fixed_topology=False)
        if panel._signature(current) != context.get("signature") or payload.get("request") != frozen:
            return False
        from coat_sim.core.film_synthesis_manufacturing import manufacturing_source_fingerprint
        source_design = frozen.get("source_design")
        if (not isinstance(source_design, Mapping) or frozen.get("source_fingerprint")
                != manufacturing_source_fingerprint(synthesis, source_design)):
            return False
        # Frozen content must still match every live source/candidate reference;
        # do not silently apply a same-ID material that changed during the job.
        base = synthesis_base_request(synthesis)
        verify_synthesis_file_references(self.service, base, source_design)
        verify_synthesis_file_references(self.service, base, snapshot["design"])
        return True

    def _apply_manufacturing_candidate(self, payload, context, snapshot) -> bool:
        session = self.session
        if not self._manufacturing_can_apply(payload, context, snapshot, full_validation=True) or session is None:
            return False
        apply = getattr(self._source_controller, "applySynthesizedDesign", None)
        if not callable(apply):
            return False
        accepted = bool(apply(deepcopy(dict(snapshot["design"])), context["revision"], context["epoch"],
                              deepcopy(context["editable_snapshot"])))
        if accepted:
            current = session.snapshot()
            self._source_revision = int(current.design_revision)
            self._design, self._path = deepcopy(current.design), str(current.path or "")
            self._source_stale = False
            self._error = ""
            self.changed.emit()
        return accepted

    def _apply_sensitivity_candidate(self, result, context) -> bool:
        """Apply a current ideal candidate, fenced by both source edit versions."""
        from coat_sim.core.film_ideal_sensitivity import FilmIdealSensitivityResult

        if not isinstance(result, FilmIdealSensitivityResult) or result.schema_version != 3:
            return False
        if result.policy == "strict_nominal":
            if result.best_analysis is None or result.best_analysis.nominal_targets_met is not True:
                return False
        elif result.policy == "bounded_baseline_degradation":
            if result.usable_candidate is not True or result.guard_pass is not True:
                return False
        else:
            return False
        if (self._busy or self._source_closed or self.session is None
                or self._source_controller is None or context is None
                or result.state not in {"completed", "partial"}
                or not result.is_final or result.operation != "optimize"
                or result.best_design is None):
            return False
        if context.get("workflow") != "ideal":
            return False
        compute, operation = context["project_compute"], context["project_operation"]
        if compute is not None and (compute.editing_frozen or not compute.accepts(operation)):
            return False
        if (self.session.snapshot().design_revision != context["revision"]
                or self._source_edit_epoch() != context["epoch"]
                or result.design_revision != context["revision"]):
            return False
        request = result.frozen_request
        frozen = FilmOptimizationRequest(request.design_snapshot, request.design_revision,
            (), request.objectives, frozen_material_snapshot=request.frozen_material_snapshot)
        # A changed live optical-set definition cannot turn a replayed frozen
        # robustness candidate into a different design at application time.
        verify_synthesis_file_references(self.service, frozen, result.best_design)
        apply = getattr(self._source_controller, "applyOptimizedDesign", None)
        return bool(callable(apply) and apply(deepcopy(dict(result.best_design)),
                    context["revision"], context["epoch"]))

    def _attach_source_observers(self) -> None:
        source_controller = self._source_controller
        mode_requested = getattr(source_controller, "optimizationModeRequested", None)
        if mode_requested is not None:
            mode_requested.connect(self.requestSearchMode)
            self._source_connections.append((mode_requested, self.requestSearchMode))
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

    def _export_objective_editor_rows(self) -> list[dict]:
        rows = deepcopy(self._objectives)
        for row in rows:
            for group in ("mean", "pointwise"):
                drafts = {field.split(".", 1)[1]: literal
                    for (objective_id, field), literal in self._objective_range_drafts.items()
                    if objective_id == row["id"] and field.startswith(group + ".")}
                if drafts:
                    row[group]["bounds_drafts"] = drafts
        return rows

    @Slot(result="QVariantMap")
    def exportOptimizationConfiguration(self) -> dict:
        """Return JSON-only editor state; commit an angle draft or reject export."""
        if self._search_mode == "synthesis":
            synthesis_structure_search_seed(self._synthesis_structure_search_seed_text)
        progress_policy = self._synthesis_progress_draft.policy_payload()
        if not self._commit_pending_angle_draft():
            raise ValueError(self._error or "请先完成角度输入")
        if self._pending_edit_error or self._objective_bounds_pending():
            raise ValueError(self._error or "请先修正尚未提交的优化输入")
        return {
            "version": 2,
            "objectives": self._export_objective_editor_rows(),
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
            "synthesis": {"options": {**self._synthesis_options, "objective_progress_policy": progress_policy},
                          "objective_progress_editor": self._synthesis_progress_draft.to_configuration(),
                          "cpu_workers": self._synthesis_cpu_workers,
                          "forward_backend": self._synthesis_forward_backend,
                          "forward_device": deepcopy(self._synthesis_forward_device),
                          "use_current_seed": self._synthesis_use_current_seed,
                          "assume_unknown_k_zero": self._synthesis_assume_unknown_k_zero,
                          "material_rows": deepcopy(self._synthesis_material_rows)},
            "search_mode": self._search_mode,
            "structure_mode": self._structure_mode,
            "reduction_mode": self._reduction_mode,
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
                       "structure_mode", "reduction_mode", "structure_options", "structure_materials", "synthesis"}
            if (set(configuration) - allowed or type(configuration.get("version")) is not int
                    or configuration.get("version") != 2):
                raise ValueError("优化配置版本或字段不受支持")
            defaults = deepcopy(self._configuration_defaults)
            raw_objectives = configuration.get("objectives", defaults["objectives"])
            if not isinstance(raw_objectives, list):
                raise ValueError("优化目标必须是数组")
            objectives, ids = [], set()
            target_fields = set(_objective_default(0))
            for index, raw in enumerate(raw_objectives):
                if (not isinstance(raw, dict) or set(raw) - target_fields
                        or not {"pointwise", "mean"}.issubset(raw)):
                    raise ValueError(f"目标 {index + 1} 字段无效；需要当前版本的两组约束")
                row = {**_objective_default(index), **deepcopy(raw)}
                if not isinstance(row["id"], str) or not row["id"] or row["id"] in ids:
                    raise ValueError("目标 ID 必须为非空且不重复的字符串")
                ids.add(row["id"])
                row = _resolve_objective_sampling(row)
                row = _resolve_angle_sampling(row)
                if (row["polarization"] not in {"s", "p", "unpolarized"}
                        or row["metric"] not in {"R", "T"}):
                    raise ValueError(f"目标 {index + 1} 偏振或指标无效")
                for group in ("mean", "pointwise"):
                    constraint = row[group]
                    fields = set(_constraint_editor_default(True))
                    if (not isinstance(constraint, dict) or set(constraint) - fields - {"bounds_drafts"}
                            or not fields.issubset(constraint)):
                        raise ValueError(f"{group} 约束字段无效")
                    if any(type(constraint[key]) is not bool for key in ("enabled", "strict_lower", "strict_upper")):
                        raise ValueError(f"{group} 开关必须为布尔值")
                    if constraint["mode"] not in {"value", "range"}:
                        raise ValueError(f"{group} 约束模式无效")
                    for bound_field in ("lower_bound_percent", "upper_bound_percent"):
                        constraint[bound_field] = _number(constraint[bound_field], bound_field)
                        if not 0 <= constraint[bound_field] <= 100:
                            raise ValueError("R/T 百分数必须在 0 到 100 之间")
                    if constraint["target_value_percent"] is not None:
                        constraint["target_value_percent"] = _number(constraint["target_value_percent"], "目标值%")
                        if not 0 <= constraint["target_value_percent"] <= 100:
                            raise ValueError("目标值必须在 0 到 100% 之间")
                    if constraint["mode"] == "value":
                        if constraint["target_value_percent"] is None:
                            raise ValueError("精确值模式需要目标值")
                        constraint["lower_bound_percent"] = constraint["upper_bound_percent"] = constraint["target_value_percent"]
                    drafts = constraint.get("bounds_drafts", {})
                    if (not isinstance(drafts, dict) or set(drafts) - {"lower_bound_percent", "upper_bound_percent"}
                            or any(not isinstance(value, str) for value in drafts.values())):
                        raise ValueError("上下限草稿字段无效")
                    if constraint["enabled"] and drafts:
                        raise ValueError("启用的约束含未提交的上下限草稿")
                _validate_editor_criteria(row)
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
            # Saved choices apply only to their exact side/layer ID. Unseen
            # layers retain the current default, including after synthesis.
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
                upper = (None if raw["upper_nm"] is None
                         else _number(raw["upper_nm"], "厚度上界"))
                if lower < 0 or (upper is not None and upper <= lower):
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
                    parts.append(f"{len(new_layers)} 个新膜层已使用默认约束；可优化层默认勾选。")
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
            reduction_mode = configuration.get("reduction_mode", "combined")
            if reduction_mode not in {"layer_count", "thickness", "combined"}:
                raise ValueError("减层 / 减厚模式无效")
            if search_mode not in {"fixed_topology", "structure", "synthesis", "reduction"} or structure_mode not in {"combined", "needle_only", "growth_only"}:
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
            synthesis_config = configuration.get("synthesis", {})
            if not isinstance(synthesis_config, dict) or set(synthesis_config) - {
                    "options", "cpu_workers", "forward_backend", "forward_device",
                    "use_current_seed", "material_rows", "assume_unknown_k_zero", "objective_progress_editor"}:
                raise ValueError("Invalid synthesis configuration")
            synthesis_values = synthesis_options(synthesis_config.get("options", {}))
            progress_draft = ObjectiveProgressDraft.from_configuration(
                synthesis_config.get("objective_progress_editor"), synthesis_values["objective_progress_policy"])
            synthesis_values.setdefault("randomize_template_search", False)
            synthesis_values.setdefault("structure_search_seed", None)
            synthesis_workers = synthesis_cpu_workers(synthesis_config.get("cpu_workers", 0))
            synthesis_forward_backend = synthesis_config.get("forward_backend", "cpu")
            synthesis_forward_device = synthesis_config.get("forward_device")
            from coat_sim.core.film_synthesis_forward import validate_forward_device
            if not isinstance(synthesis_forward_backend, str):
                raise ValueError("Synthesis forward backend must be a string")
            synthesis_forward_device = validate_forward_device(
                synthesis_forward_device, synthesis_forward_backend)
            if synthesis_forward_backend != "cpu" and synthesis_forward_device is None:
                raise ValueError("Saved GPU forward selection has no verified device binding")
            synthesis_seed = synthesis_config.get("use_current_seed", False)
            synthesis_assumption = synthesis_config.get("assume_unknown_k_zero", False)
            if type(synthesis_assumption) is not bool:
                raise ValueError("未知 k 按 0 假设必须为明确的开关")
            if type(synthesis_seed) is not bool:
                raise ValueError("Synthesis seed selection must be explicit")
            synthesis_rows = synthesis_config.get("material_rows")
            if synthesis_rows is not None:
                if not isinstance(synthesis_rows, list):
                    raise ValueError("Synthesis material rows must be a list")
                row_ids = set()
                for material_row in synthesis_rows:
                    if (not isinstance(material_row, dict)
                            or set(material_row) != {"id", "material_id", "optical_set_id"}
                            or any(not isinstance(value, str) for value in material_row.values())
                            or not material_row["id"] or material_row["id"] in row_ids):
                        raise ValueError("Synthesis material rows require unique stable IDs and typed references")
                    row_ids.add(material_row["id"])
                synthesis_rows = deepcopy(synthesis_rows)
            refs = configuration.get("structure_materials", [])
            if not isinstance(refs, list):
                raise ValueError("结构候选材料必须为数组")
            selected_refs = set()
            available_refs = {(row["material_id"], row["optical_set_id"])
                              for row in self._structure_material_options}
            if search_mode == "synthesis":
                available_refs = {(material["id"], optical["id"])
                    for material in cast(_SynthesisMaterialService, self.service).material_options("film")
                    if material.get("design_ready") is not False
                    for optical in material.get("optical_sets", [])}
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
        if synthesis_rows is None:
            synthesis_rows = [{"id": uuid4().hex, "material_id": row["material_id"],
                               "optical_set_id": row["optical_set_id"]} for row in refs]
        self._configuration_loading = True
        try:
            self._angle_draft = None
            self._objective_range_drafts.clear()
            self._objective_range_errors.clear()
            self._objective_range_display.clear()
            for objective in objectives:
                for group in ("mean", "pointwise"):
                    for field, literal in objective[group].pop("bounds_drafts", {}).items():
                        self._objective_range_drafts[(objective["id"], f"{group}.{field}")] = literal
            self._objectives, self._variables = objectives, variables
            self._selected_objective = 0 if objectives else -1
            self._plot_objective, self._plot_angle = 0, 0.0
            self._solver_id, self._acceptable_error_percent = solver["method"], acceptable
            for name in ("max_iterations", "max_evaluations", "seed", "validation_samples_multiplier",
                         "multistart_count", "population_size"):
                setattr(self, "_" + name, solver[name])
            self._search_mode, self._structure_mode = search_mode, structure_mode
            self._reduction_mode = reduction_mode
            self._compute_target.setTask("film_synthesis" if search_mode == "synthesis" else "film_optimization")
            self._synthesis_options, self._synthesis_use_current_seed = synthesis_values, synthesis_seed
            search_seed = synthesis_values["structure_search_seed"]
            self._synthesis_structure_search_seed_text = "" if search_seed is None else str(search_seed)
            self._synthesis_structure_search_seed_error = ""
            self._synthesis_progress_draft = progress_draft
            self._synthesis_progress_error = ""
            self._synthesis_progress_edit_dirty = False
            self._synthesis_actual_structure_search_seed = None
            self._synthesis_cpu_workers = synthesis_workers
            self._synthesis_forward_backend = synthesis_forward_backend
            self._synthesis_forward_device = deepcopy(synthesis_forward_device)
            self._synthesis_assume_unknown_k_zero = synthesis_assumption
            self._synthesis_material_rows = synthesis_rows
            self._synthesis_rows_initialized = True
            self._structure_options = structure
            if search_mode == "synthesis":
                self._refresh_structure_material_options(preserve_selection=True)
                if synthesis_forward_backend != "cpu":
                    self._synthesis_forward_probe.probe()
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
        try:
            configuration = self.exportOptimizationConfiguration()
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
        self._live_display.shutdown()
        if self._sensitivity_controller is not None:
            self._sensitivity_controller.detach()
        if self._manufacturing_controller is not None:
            self._manufacturing_controller.detach()
        self._synthesis_forward_probe.cancel()
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
            self._request_optimization_cancel(user_initiated=False)

    def _source_edit_epoch(self) -> int | None:
        if self._source_controller is None:
            return None
        try:
            return int(getattr(self._source_controller, "pendingEditEpoch", 0))
        except (TypeError, ValueError, RuntimeError):
            return None

    @Slot()
    def _on_source_pending_edit(self) -> None:
        if self._manufacturing_controller is not None:
            self._manufacturing_controller.sourceChanged()
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
                    if physical_nm < 0:
                        physical_nm = None
                except (TypeError, ValueError):
                    physical_nm = None
                if old is not None:
                    low = old["lower_nm"]
                    high = old["upper_nm"]
                    enabled = bool(old["enabled"])
                else:
                    low, high, enabled = 0.0, None, physical_nm is not None
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
                    "upper_nm": None if high is None else float(high),
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
                    parts.append(f"{len(added)} 个新膜层已使用默认约束；可优化层默认勾选。")
                self._configuration_restore_notice = "".join(parts)
        self._variables = variables
        self._source_document_replaced = False
        self._source_stale = False
        if not self._initial_loading:
            self._refresh_structure_material_options(preserve_selection=preserve_variables)
        self.changed.emit()
        self.plotDataChanged.emit()

    def _refresh_structure_material_options(self, *, preserve_selection: bool,
                                            records: list[dict] | None = None) -> None:
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
            if records is None:
                records = catalog._records() if callable(getattr(catalog, "_records", None)) else None
            if records is None:
                raise ValueError("膜材目录不支持枚举")
            if self._search_mode == "synthesis":
                # Project snapshots have no role/category fields. Only the
                # service's explicit film-purpose projection grants eligibility.
                records = [{**material, "roles": ["film"]}
                           for material in cast(_SynthesisMaterialService, self.service).material_options("film")]
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
        if not self._synthesis_rows_initialized and any(row.get("selected") for row in rows):
            self._synthesis_material_rows = [{"id": uuid4().hex, "material_id": row["material_id"],
                "optical_set_id": row["optical_set_id"]} for row in rows if row.get("selected")]
            self._synthesis_rows_initialized = True

    def _on_session_change(self, change) -> None:
        if self._configuration_loading:
            return
        snapshot = self.session.snapshot()
        if (self._manufacturing_controller is not None
                and snapshot.design_revision != self._source_revision):
            self._manufacturing_controller.sourceChanged()
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
            if self._candidate_applied and not self._busy and not self._pending_edit_error:
                # An applied result no longer protects an unapplied proposal.
                # Undo/subsequent committed edits must rebuild the variable
                # model too; in-flight or unapplied conflicts stay fenced.
                self._configuration_loading = True
                try:
                    self._invalidate_result()
                    self._load_source(preserve_variables=True)
                finally:
                    self._configuration_loading = False
                self._restore_session_configuration(reset_missing=False)
            else:
                self._source_stale = True
                self.changed.emit()

    def _invalidate_result(self) -> None:
        if self._manufacturing_controller is not None:
            self._manufacturing_controller.sourceChanged()
        self._synthesis_actual_structure_search_seed = None
        self._live_display.clear_analysis()
        self._sync_film_execution_target()
        if not self._configuration_loading:
            self._configuration_load_error = ""
        self._result = None
        self._synthesis_payload = None
        self._synthesis_editable_snapshot = None
        self._structure_result = None
        self._reduction_result = None
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
        if not 0 <= self._selected_objective < len(self._objectives):
            return {}
        row = deepcopy(self._objectives[self._selected_objective])
        for field in _OBJECTIVE_BOUNDS_FIELDS:
            key = (row["id"], field)
            draft = self._objective_range_drafts.get(key, self._objective_range_display.get(key))
            if draft is not None:
                if "." in field:
                    group, bound = field.split(".", 1)
                    row[group][bound] = draft
                else:
                    row[field] = draft
        return row

    def _objective_draft_is_active(self, objective_id: str, field: str) -> bool:
        if "." not in field:
            return True
        group = field.split(".", 1)[0]
        row = next((row for row in self._objectives if row["id"] == objective_id), None)
        return row is not None and bool(row[group]["enabled"])

    @Property(str, notify=changed)
    def objectiveBoundsError(self) -> str:
        return self._selected_objective_bounds_error()

    def _selected_objective_bounds_error(self) -> str:
        if not 0 <= self._selected_objective < len(self._objectives):
            return ""
        objective_id = self._objectives[self._selected_objective]["id"]
        return "\n".join(message for (oid, group), message in self._objective_range_errors.items()
            if oid == objective_id and (group == "wavelength" or self._objective_draft_is_active(oid, group + ".lower_bound_percent")))

    def _objective_bounds_pending(self) -> bool:
        return any(self._objective_draft_is_active(oid, field)
                   for oid, field in self._objective_range_drafts) or self._objective_bounds_invalid()

    def _objective_bounds_invalid(self) -> bool:
        return any(group == "wavelength" or self._objective_draft_is_active(oid, group + ".lower_bound_percent")
                   for oid, group in self._objective_range_errors)

    @Slot(str, str)
    def setObjectiveBoundsDraft(self, field: str, text: str) -> None:
        """Keep literal text through focus changes without rebuilding controls."""
        if (self._busy or self._shutting_down or field not in _OBJECTIVE_BOUNDS_FIELDS
                or not 0 <= self._selected_objective < len(self._objectives)):
            return
        row = self._objectives[self._selected_objective]
        self._objective_range_drafts[(row["id"], field)] = str(text)
        self.changed.emit()

    @Slot(str, str, result=bool)
    def commitObjectiveBoundsDraft(self, field: str, text: str) -> bool:
        if (self._busy or self._shutting_down or field not in _OBJECTIVE_BOUNDS_FIELDS
                or not 0 <= self._selected_objective < len(self._objectives)):
            return False
        index = self._selected_objective
        row = self._objectives[index]
        objective_id = row["id"]
        group = field.split(".", 1)[0] if "." in field else "wavelength"
        drafts = self._objective_range_drafts
        drafts[(objective_id, field)] = str(text)
        updated = deepcopy(row)
        if group != "wavelength" and not row[group]["enabled"]:
            self.changed.emit()
            self._persist_configuration()
            return True
        pair = (tuple(f"{group}.{name}_bound_percent" for name in ("lower", "upper"))
                if group != "wavelength" else ("wavelength_min_nm", "wavelength_max_nm"))
        try:
            if group != "wavelength":
                constraint = updated[group]
                lower, upper = (_number(drafts.get((objective_id, key), constraint[key.split(".")[1]]), key)
                                for key in pair)
                if not 0 <= lower <= upper <= 100:
                    raise ValueError("目标上下限必须满足 0 ≤ 下限 ≤ 上限 ≤ 100%。")
                constraint.update(lower_bound_percent=lower, upper_bound_percent=upper,
                                  mode="range", target_value_percent=None)
                _validate_editor_criteria(updated)
            else:
                low, high = (_number(drafts.get((objective_id, key), row[key]), key) for key in pair)
                if low <= 0 or low > high:
                    raise ValueError("波长范围必须满足 0 < 下限 ≤ 上限（nm）。")
                updated = _resolve_objective_sampling({**updated, pair[0]: low, pair[1]: high}, edited_field=field)
        except (TypeError, ValueError, OverflowError) as exc:
            self._objective_range_errors[(objective_id, group)] = str(exc)
            self._error = str(exc)
            self.changed.emit()
            return False
        self._objectives[index] = updated
        for key in pair:
            literal = drafts.pop((objective_id, key), None)
            if literal is not None:
                self._objective_range_display[(objective_id, key)] = literal
        self._objective_range_errors.pop((objective_id, group), None)
        self._error = self._selected_objective_bounds_error()
        self._invalidate_result()
        return True

    @Slot(str, bool, result=bool)
    def setObjectiveConstraintEnabled(self, group: str, enabled: bool) -> bool:
        if (self._busy or self._shutting_down or group not in {"mean", "pointwise"}
                or type(enabled) is not bool or not 0 <= self._selected_objective < len(self._objectives)):
            return False
        row = self._objectives[self._selected_objective]
        row[group]["enabled"] = enabled
        # Preserve half typed disabled text. Enabling restores it for correction;
        # active drafts prevent submitting or exporting until committed.
        self._error = ""
        self._invalidate_result()
        return True

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
            {"id": "reduction", "label": "减层 / 减厚优化"},
            {"id": "synthesis", "label": "从零自动合成"},
            {"id": "structure", "label": "结构合成"},
        ]

    @Property(str, notify=changed)
    def reductionMode(self) -> str:
        return self._reduction_mode

    @Property(list, notify=changed)
    def reductionModeOptions(self) -> list[dict]:
        return [{"id": "layer_count", "label": "仅减层"},
                {"id": "thickness", "label": "仅减厚"},
                {"id": "combined", "label": "联合减层 / 减厚"}]

    @Property(str, notify=changed)
    def reductionPolicySummary(self) -> str:
        selected = [row for row in self._variables if row["enabled"] and row["available"]]
        deletable = sum(row["lower_nm"] == 0 for row in selected)
        protected = sum(row["lower_nm"] > 0 for row in selected)
        modes = {
            "layer_count": "仅减层：删除 / 合并后在原边界内调整已选保留层厚度以恢复全部光学目标；以层数减少为准，总厚度可以增加。",
            "thickness": "仅减厚：保持层序，以总厚度减少为准。",
            "combined": "联合减量：层数和总厚度均不得高于原设计，至少一项严格减少。",
        }
        rule = ("沿用全部原始光学约束与已选厚度变量。仅下界明确为 0 nm 的已选层可删除 / 合并；"
                "下界大于 0 nm 的层不可删除。参与合并的每层都须允许删除，且材料和光学数据集完全相同；"
                "保留层仍受原边界约束，不新增材料。完成或取消均不会自动应用。")
        note = f"已选允许删除 {deletable} 层；下界大于 0 的已选保留层 {protected} 层。"
        if self._reduction_mode == "combined" and not deletable:
            note += "当前没有可删除层，联合模式仍可尝试减厚。"
        return modes[self._reduction_mode] + rule + note

    @Property(bool, notify=changed)
    def hasReductionResult(self) -> bool:
        return self._reduction_result is not None

    @Slot(int)
    def _on_film_display_precision(self, _decimals: int) -> None:
        self.changed.emit()

    @Property(int, notify=changed)
    def filmThicknessDecimals(self) -> int:
        from .theme_service import ThemeService
        return ThemeService.instance().film_thickness_decimals

    @Property(str, notify=changed)
    def reductionComparison(self) -> str:
        reduction = self._reduction_result
        if reduction is None:
            return f"{self.reductionPolicySummary}原始全部目标及独立加密网格均通过且确有减量的候选才可显式应用。"
        lines = []
        for key, label in (("baseline", "原设计"), ("candidate", "候选")):
            row = reduction.metrics[key]
            def fmt(value):
                return "—" if value is None else f"{value:g}"
            lines.append(f"{label}：{row['layer_count']} 层 · 总厚 {format_film_thickness(row['total_thickness_nm'])} nm · 最薄 {format_film_thickness(row['min_thickness_nm']) + ' nm' if row['layer_count'] else '无膜层'} · 最差误差 {fmt(row['max_normalized_error'])} · 加密最差误差 {fmt(row['validation_max_normalized_error'])} · 原目标 {'通过' if row['targets_met'] else '未通过'} · 独立加密 {'通过' if row['validation_targets_met'] else '未通过'}")
            for side, name in (("front", "正面"), ("back", "背面")):
                value = row[side]
                lines.append(f"  {name}：{value['layer_count']} 层 · {format_film_thickness(value['total_thickness_nm'])} nm · 最薄 {format_film_thickness(value['min_thickness_nm']) + ' nm' if value['layer_count'] else '无膜层'}")
        if self._candidate_applied:
            lines.append("候选已显式应用；可在来源设计中撤销本次应用。")
        elif self.canApply:
            lines.append("候选已通过独立审计且有减量，可显式应用。")
        elif self._reduction_eligible():
            lines.append("候选已通过独立审计且有减量；当前来源或历史查看状态不允许应用，可查看或另存。")
        else:
            lines.append("候选仅供查看：尚未同时满足完整原目标、独立加密审计与减量条件。")
        return "\n".join(lines)

    def _reduction_eligible(self) -> bool:
        value = self._reduction_result
        return bool(value is not None and value.candidate_verified
                    and value.summary.get("candidate_feasible") is True
                    and value.summary.get("reduction_achieved") is True)

    @Slot(str, result=bool)
    def setReductionMode(self, mode: str) -> bool:
        if self._busy or self._shutting_down or self._pending_edit_error or mode not in {"layer_count", "thickness", "combined"}:
            return False
        if mode != self._reduction_mode:
            self._reduction_mode = mode
            self._invalidate_result()
        return True

    @Slot(str, result=bool)
    def requestSearchMode(self, mode: str) -> bool:
        if self._source_controller is not None:
            self._source_controller._requested_optimizer_search_mode = ""
        self._requested_search_mode = mode if self._initial_loading else ""
        if self._initial_loading:
            return True
        # Keep an existing unsaved candidate available for review.
        if self._result is not None and not self._candidate_saved and not self._candidate_applied:
            if self._search_mode != mode:
                self._requested_search_mode = mode
                self._error = "请先保存、应用或清除当前候选，再切换优化模式；当前候选已保留。"
                self.changed.emit()
                return False
            return True
        return self.setSearchMode(mode)

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
        raw = next((row for row in self._result.raw_coverage
            if row.objective_index == self._plot_objective
            and math.isclose(row.angle_deg, self._plot_angle, abs_tol=1e-10)), None)
        if raw is None:
            return {}
        original = self._baseline.get((self._plot_objective, self._plot_angle), {})
        criterion = objective["pointwise"]
        return {
            "wavelength_nm": list(raw.wavelength_nm),
            "baseline": list(original.get("actual", [])),
            "baseline_label": "N=0 空栈基线" if self._synthesis_payload is not None else "原设计",
            "candidate": list(raw.actual_values),
            "metric": objective["metric"],
            "mode": criterion["mode"],
            "target_value": (float(criterion["target_value_percent"]) / 100.0
                             if criterion["enabled"] and criterion["mode"] == "value" else None),
            "lower_bound": (float(criterion["lower_bound_percent"]) / 100.0
                            if criterion["enabled"] and criterion["mode"] == "range" else None),
            "upper_bound": (float(criterion["upper_bound_percent"]) / 100.0
                            if criterion["enabled"] and criterion["mode"] == "range" else None),
        }

    @staticmethod
    def _criterion_status_text(status: dict) -> str:
        lines = []
        for group, title in (("mean", "平均值"), ("pointwise", "绝对值（逐点）")):
            item = status[group]
            if not item["enabled"]:
                lines.append(f"{title}：未启用")
                continue
            if item["mode"] == "value":
                bounds = f"= {item['target_value'] * 100:.6g}%"
            else:
                low, high = item["lower_bound"], item["upper_bound"]
                bounds = (f"{'>' if item['strict_lower'] else '≥'} {low * 100:.6g}%" if low is not None else "")
                if high is not None:
                    bounds += ("，" if bounds else "") + f"{'<' if item['strict_upper'] else '≤'} {high * 100:.6g}%"
            worst = f"{item['worst_angle_deg']:g}°"
            if item["worst_wavelength_nm"] is not None:
                worst += f" / {item['worst_wavelength_nm']:.6g} nm"
            lines.append(f"{title}：{'通过' if item['targets_met'] else '未通过'}（{bounds}）；"
                f"最差角 {worst}，实际 {item['worst_actual'] * 100:.6g}%")
        return "\n".join(lines)

    @Property(str, notify=changed)
    def objectiveCriteriaSummary(self) -> str:
        if self._result is None or self._frozen_request is None:
            return ""
        from coat_sim.core.film_synthesis_audit import objective_constraint_status
        errors = (self._result.validation_per_condition_errors
                  if self._result.validation_complete else self._result.per_condition_errors)
        if not errors:
            return ""
        statuses = objective_constraint_status(self._frozen_request.objectives, errors,
            self._frozen_request.acceptable_normalized_error)
        scope = "独立验证" if self._result.validation_complete else "仅训练网格（未完成独立验证）"
        return scope + "\n" + "\n".join(f"目标 {row['objective_index'] + 1} · {row['metric']} · "
            f"{row['wavelength_min_nm']:g}–{row['wavelength_max_nm']:g} nm\n"
            + self._criterion_status_text(row) for row in statuses)

    @Property("QVariantList", notify=changed)
    def conditionErrors(self) -> list[dict]:
        if self._result is None:
            return []
        rows = sorted((self._result.validation_per_condition_errors if self._synthesis_payload is not None
                       else self._result.per_condition_errors),
                      key=lambda row: row.normalized_violation, reverse=True)[:24]
        return [{
            "objective": row.objective_index + 1,
            "wavelength": (f"{row.wavelength_nm:.4g} nm" if row.wavelength_nm is not None
                           else f"{row.wavelength_min_nm:g}–{row.wavelength_max_nm:g} nm · 平均值"),
            "reduction": row.reduction,
            "angle": f"{row.angle_deg:g}°",
            "polarization": row.polarization,
            "metric": row.metric,
            "actual": f"{row.actual * 100:.4g}%",
            "target": self._condition_target(row),
            "violation": f"{row.normalized_violation * 100:.4g}%",
            "normalized_violation": row.normalized_violation,
        } for row in rows]

    @Property(str, notify=changed)
    def conditionErrorsEmptySummary(self) -> str:
        if self._result is None:
            return "完成一次计算后显示实际值与目标差异。"
        if self._synthesis_payload is not None:
            raw = self._synthesis_payload.get("result")
            audit = self._synthesis_payload.get("best_candidate_audit")
            if (not isinstance(raw, dict) or not isinstance(audit, dict)
                    or raw.get("validation_complete") is not True
                    or audit.get("available") is not True):
                return "没有完整独立审计；目标状态未验证。"
            if audit.get("targets_met") is True:
                return "独立离散审计已完成；所有条件均达标。"
            if audit.get("targets_met") is False:
                return "独立离散审计已完成；仍有条件未达标。"
            return "独立审计状态未知；不能确认已达标。"
        return ("所有条件已达标。" if self._result.targets_met
                else "当前候选未满足所有目标。")

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
        return bool(self._busy or (self._sensitivity_controller is not None
                                   and self._sensitivity_controller.busy)
                    or (self._manufacturing_controller is not None and self._manufacturing_controller.busy))

    @Property(float, notify=changed)
    def progress(self) -> float:
        return self._progress

    @Property(str, notify=changed)
    def progressText(self) -> str:
        return self._progress_text

    @Property(str, notify=changed)
    def progressStatistics(self) -> str:
        if self._synthesis_payload is not None:
            return (f"轮次 {self._synthesis_work_count('cycles')} · "
                    f"评价 {self._synthesis_work_count('evaluations')}" + self._synthesis_work_counters_note())
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
        text = self._status_text()
        notice = self._numeric_cleanup_notice()
        return text + "\n" + notice if notice else text

    def _status_text(self) -> str:
        if self._initial_loading:
            return "正在加载膜系优化组件与材料，请稍候…"
        if self._structure_material_error:
            return "材料读取失败：" + self._structure_material_error
        if (self._inspection_objectives is not None
                and (self._result_terminal_state == "队列历史结果"
                     or self._result_terminal_state.startswith("历史合成结果 / "))):
            return "只读历史候选；不会应用到当前设计。"
        if self._result_terminal_state in {"已取消", "失败"}:
            if self._result is not None:
                if self._result_terminal_state == "已取消" and self._candidate_applied:
                    if self._error:
                        return self._error
                    return "优化已取消；最佳候选已应用到当前膜系，未保存，并已请求刷新光谱。"
                status = (f"优化{self._result_terminal_state}；已保留队列验证的最佳候选，"
                          "可查看或另存，不能应用。")
                return status + (" " + self._error if self._error else "")
            if self._result_terminal_state == "失败" and self._error:
                return f"优化失败；没有可用的验证候选，原设计未改变。{self._error}"
            return "优化已取消；队列没有可用的验证候选，原设计未改变。"
        if self._result is None and self._result_terminal_state == "cancelled":
            return "优化已取消；没有返回可用候选，原设计未改变。"
        if self._result is None and self._result_terminal_state == "not_found":
            return "未找到可行候选；原设计未改变。"
        if self._result is None and self._result_terminal_state == "failed":
            return "优化失败；没有返回可用候选，原设计未改变。" + (" " + self._error if self._error else "")
        if self._error:
            return self._error
        if self._source_stale:
            return "原设计已修改；先刷新设计，再开始优化或应用候选。"
        configuration_error = self._active_configuration_error()
        if configuration_error:
            return configuration_error
        if not self._objectives:
            return "至少添加一个目标后才能优化。"
        if (self._search_mode in {"fixed_topology", "reduction"}
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
            if self._candidate_applied:
                return "优化已取消；最佳候选已应用到当前膜系，未保存，并已请求刷新光谱。"
            return "优化已取消；最佳候选仍可预览或另存。"
        if self._result.targets_met:
            if self._candidate_applied:
                return "已满足所有采样目标；候选已应用，未保存，并已请求刷新光谱。"
            return "已满足所有采样目标；可预览、另存或显式应用。"
        if self._candidate_applied:
            return "未全部达标；候选已应用，未保存，并已请求刷新光谱。"
        return "当前候选未满足所有目标；仍可预览或另存。"

    @Property(bool, notify=changed)
    def canStart(self) -> bool:
        has_search_inputs = (
            any(row["enabled"] and row["available"] for row in self._variables)
            if self._search_mode in {"fixed_topology", "reduction"}
            else (self._search_mode != "synthesis" and self._structure_mode == "growth_only"
                  or any(row.get("selected") for row in self._structure_material_options)))
        return (not self._initial_loading and not self._initial_load_failed
                and not self._structure_material_error
                and not self.busy and not self._source_closed and not self._source_stale
                and bool(self._objectives) and has_search_inputs
                and not self._pending_edit_error
                and not self._objective_bounds_invalid()
                and not self._active_configuration_error())

    @Property(bool, notify=changed)
    def canStartFromShortcut(self) -> bool:
        return (not self._initial_loading and not self._initial_load_failed
                and not self._structure_material_error
                and not self.busy and not self._source_closed
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
        return bool(self.session is not None and not self._source_closed and self._result and not self.busy
                    and not self.candidateStale
                    and not self._candidate_applied
                    and self._result_application_eligible)

    @Property(bool, notify=changed)
    def canSave(self) -> bool:
        return self._result is not None and not self._busy

    @Property(bool, notify=changed)
    def canCancel(self) -> bool:
        return bool(self._busy or (self._manufacturing_controller is not None and self._manufacturing_controller.busy))

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
        if self._search_mode == "reduction":
            if (self._reduction_mode == "layer_count" and not any(
                    row["enabled"] and row["available"] and row["lower_nm"] == 0
                    for row in self._variables)):
                return "仅减层需要至少一个已选且下界明确为 0 nm 的可删除层；下界大于 0 的层不可删除。"
            if self._validation_samples_multiplier < 2:
                return "减量优化要求独立加密验证倍率至少为 2；请在算法页调整。"
            if self._max_evaluations < 2:
                return "减量优化至少需要两次完整评估以审计原设计。"
            return self._error if self._pending_edit_error else ""
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
        if self._synthesis_payload is not None:
            return self._synthesis_audit_summary()
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
        if self._synthesis_payload is not None:
            return ("方法 从零自动合成 · "
                    f"失败评价 {self._synthesis_work_count('failed_evaluations')} · "
                    f"审计候选评价 {self._synthesis_work_count('validation_evaluations')}"
                    + self._synthesis_work_counters_note())
        result = self._result
        if result is None:
            return ""
        method = (_SOLVER_UI_CHOICES[_SOLVER_UI_INDEX[result.solver]][1]
                  if result.solver in _SOLVER_UI_INDEX else result.solver)
        reason = {"target_met": "训练目标达标", "evaluation_budget": "评估预算用尽",
                  "iteration_limit": "迭代预算用尽", "converged": "搜索停止",
                  "cancelled": "已取消", "completed": "已完成", "failed": "失败"}.get(
                      result.termination_reason or result.state, result.termination_reason or result.state)
        text = (f"方法 {method} · 随机种子 {result.seed} · 终止 {reason}"
                f" · 失败评估 {result.failed_evaluations} · 留出评估 {result.validation_evaluations}")
        binding = getattr(self._frozen_request, "execution_device", None)
        if binding and binding["kind"] == "intel_xpu_fp64_v1":
            text += (f" · 执行绑定 Intel XPU {binding['index']} / UUID {binding['uuid']}"
                     " · FP64/complex128 前向，CPU 评分与独立验证；失败不回退 CPU")
        elif binding:
            text += f" · CPU 批量前向 {binding.get('cpu_workers', 1)} 线程 / {binding.get('cpu_parallel_axis', 'auto')}"
        return text

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
        if solver_id in _SCIPY_SOLVERS and self._initial_loading:
            return "正在检查 SciPy，请稍候"
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

    def _active_configuration_error(self, *, require_thickness_bounds: bool = True) -> str:
        if self._configuration_load_error:
            return self._configuration_load_error
        try:
            self._synthesis_progress_draft.policy_payload()
        except ValueError as exc:
            return str(exc)
        if any(row.get("angle_sampling_mode") != "range" for row in self._objectives):
            return "目标含显式角度配置；请逐个重新确认角度范围与精度，原角度未自动更改。"
        for row in self._objectives:
            try:
                _validate_editor_criteria(row)
            except (TypeError, ValueError, KeyError) as exc:
                return str(exc)
        if self._search_mode in {"fixed_topology", "reduction"} and require_thickness_bounds:
            for row in self._variables:
                if not row["enabled"]:
                    continue
                if not row["available"]:
                    return f"膜层 {row['layer_id']} 的厚度或光学数据不可用；可取消选择该层。"
                if row["upper_nm"] is None and self._solver_id != "baseline" and self._search_mode != "reduction":
                    return ("当前有限域厚度优化算法需要为已选膜层设置明确的厚度上界；"
                            "默认未设置人为上界，也不会自动补上限。")
                if (row["lower_nm"] < 0 or
                        (row["upper_nm"] is not None and row["upper_nm"] <= row["lower_nm"])):
                    return "已选膜层的厚度边界必须非负且上界大于下界。"
        try:
            training = sum(row["wavelength_samples"] * _angle_sample_count(row)
                           for row in self._objectives)
            validation = sum(((row["wavelength_samples"] - 1) * self._validation_samples_multiplier + 1)
                             * _angle_sample_count(row) for row in self._objectives)
            if training < 1 or validation < 1:
                return "目标波长或角度配置必须至少产生一个计算条件。"
        except (TypeError, ValueError, KeyError):
            return "目标波长或角度配置无效，请修正后再开始。"
        if self._compute_target.is_xpu_selected() and self._film_xpu_unavailable_reason():
            return self._film_xpu_unavailable_reason()
        if self._batch_execution_enabled() and self._compute_target.hostId != "local":
            return "延迟批量模式目前仅支持本机。"
        if self._search_mode == "structure":
            return self._structure_configuration_error()
        if self._search_mode == "synthesis":
            return self._synthesis_configuration_error()
        if self._search_mode == "reduction":
            if (self._reduction_mode == "layer_count" and not any(
                    row["enabled"] and row["available"] and row["lower_nm"] == 0
                    for row in self._variables)):
                return "仅减层需要至少一个已选且下界明确为 0 nm 的可删除层；下界大于 0 的层不可删除。"
            if self._validation_samples_multiplier < 2:
                return "减量优化要求独立加密验证倍率至少为 2；请在算法页调整。"
            if self._max_evaluations < 2:
                return "减量优化至少需要两次完整评估以审计原设计。"
            return self._error if self._pending_edit_error else ""
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
                or mode not in {"fixed_topology", "structure", "synthesis", "reduction"}):
            if mode not in {"fixed_topology", "structure", "synthesis", "reduction"}:
                self._error = "未知的优化模式。"
                self.changed.emit()
            return False
        if mode == self._search_mode:
            return True
        previous_mode = self._search_mode
        self._search_mode = mode
        self._compute_target.setTask("film_synthesis" if mode == "synthesis" else "film_optimization")
        if mode == "synthesis":
            self._synthesis_forward_probe.probe()
        if mode == "synthesis" or previous_mode == "synthesis":
            self._refresh_structure_material_options(preserve_selection=True)
        if mode == "synthesis" and not self._synthesis_rows_initialized:
            self._synthesis_material_rows = [{"id": uuid4().hex, "material_id": row["material_id"],
                "optical_set_id": row["optical_set_id"]} for row in self._structure_material_options if row.get("selected")]
            self._synthesis_rows_initialized = True
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
        if self._search_mode == "synthesis":
            matches = [item for item in self._synthesis_material_rows
                       if (item["material_id"], item["optical_set_id"]) == pair]
            if selected and not matches:
                self._synthesis_material_rows.append({"id": uuid4().hex,
                    "material_id": pair[0], "optical_set_id": pair[1]})
            elif not selected:
                self._synthesis_material_rows = [item for item in self._synthesis_material_rows if item not in matches]
            self._synthesis_rows_initialized = True
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
        if self.busy or self._shutting_down:
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
        if self._synthesis_payload is not None:
            raw = self._synthesis_payload["result"]
            facts = self._synthesis_payload["best_candidate_audit"]
            return (f"任务 {raw['state']}；原门限名义验收：{self._synthesis_acceptance_text(facts.get('targets_met'))}；"
                    f"求解容差可接受：{self._synthesis_acceptance_text(facts.get('solver_acceptable'))}；"
                    f"轮次 {self._synthesis_work_count('cycles')}；评价 {self._synthesis_work_count('evaluations')}"
                    + self._synthesis_work_counters_note())
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
        for (_side, layer_id), row in labels.items():
            if layer_id in self._result.best_thickness_nm:
                output.append({
                    "label": f"{row['side_label']} · {row['label']}",
                    "thickness": f"{format_film_thickness(self._result.best_thickness_nm[layer_id])} nm",
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
        low = "无下限" if row.lower_bound is None else f"{'>' if row.strict_lower else '≥'}{row.lower_bound * 100:.4g}%"
        high = "无上限" if row.upper_bound is None else f"{'<' if row.strict_upper else '≤'}{row.upper_bound * 100:.4g}%"
        return f"{low}，{high}"

    @Slot(int)
    def selectObjective(self, index: int) -> None:
        if (not self._busy and not self._pending_edit_error
                and 0 <= int(index) < len(self._objectives)):
            if not self._commit_pending_angle_draft():
                return
            self._selected_objective = int(index)
            self.changed.emit()

    @Slot()
    def addObjective(self) -> None:
        if (self._busy or self._shutting_down or self._pending_edit_error
                or self._objective_bounds_pending()):
            return
        if not self._commit_pending_angle_draft():
            return
        self._objectives.append(_objective_default(len(self._objectives)))
        self._selected_objective = len(self._objectives) - 1
        self._invalidate_result()

    @Slot(result=bool)
    def duplicateSelectedObjective(self) -> bool:
        if (self._busy or self._shutting_down or self._pending_edit_error
                or self._objective_bounds_pending()
                or not 0 <= self._selected_objective < len(self._objectives)):
            return False
        if not self._commit_pending_angle_draft():
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
        if (self._busy or self._shutting_down or self._pending_edit_error
                or self._objective_bounds_pending()):
            return
        if not 0 <= self._selected_objective < len(self._objectives):
            return
        if not self._commit_pending_angle_draft():
            return
        removed = self._objectives.pop(self._selected_objective)
        removed_id = str(removed.get("id", ""))
        for key in tuple(self._objective_range_display):
            if key[0] == removed_id:
                self._objective_range_display.pop(key, None)
        for key in tuple(self._objective_range_errors):
            if key[0] == removed_id:
                self._objective_range_errors.pop(key, None)
        self._selected_objective = min(self._selected_objective,
                                       len(self._objectives) - 1)
        self._plot_objective = min(self._plot_objective,
                                   max(0, len(self._objectives) - 1))
        self._invalidate_result()

    @Slot(str, str, str)
    def noteObjectiveAngleDraft(self, minimum: str, maximum: str, precision: str) -> None:
        if self._busy or self._shutting_down or not 0 <= self._selected_objective < len(self._objectives):
            return
        self._angle_draft = {"id": self._objectives[self._selected_objective]["id"],
            "minimum": minimum, "maximum": maximum, "precision": precision}

    def _commit_pending_angle_draft(self) -> bool:
        draft = self._angle_draft
        if draft is None:
            return True
        if not 0 <= self._selected_objective < len(self._objectives):
            return False
        row = self._objectives[self._selected_objective]
        if row["id"] != draft["id"]:
            return False
        if row.get("angle_sampling_mode") == "points":
            self._record_pending_edit_error(f"objective:{self._selected_objective}:angle_range",
                "原显式角度序列已保留；请明确确认角度范围与精度后再保存或开始")
            return False
        return self.confirmObjectiveAngleRange(draft["minimum"], draft["maximum"], draft["precision"])

    @Slot(str, str, str, result=bool)
    def confirmObjectiveAngleRange(self, minimum: str, maximum: str, precision: str) -> bool:
        """Explicitly replace a preserved angle list using the visible range form."""
        if self._busy or self._shutting_down or not 0 <= self._selected_objective < len(self._objectives):
            return False
        key = f"objective:{self._selected_objective}:"
        angle_keys = {key + field for field in ("angle_min_deg", "angle_max_deg", "angle_step_deg", "angle_range")}
        if self._pending_edit_error and self._pending_edit_error_key not in angle_keys:
            return False
        if self._objective_bounds_pending():
            return False
        try:
            updated = _resolve_angle_sampling({
                **self._objectives[self._selected_objective], "angle_sampling_mode": "range",
                "angle_min_deg": _number(minimum, "角度下限"),
                "angle_max_deg": _number(maximum, "角度上限"),
                "angle_step_deg": _number(precision, "角度精度"),
            })
        except (ValueError, TypeError, OverflowError) as exc:
            self._record_pending_edit_error(key + "angle_range", str(exc))
            return False
        self._angle_draft = None
        self._objectives[self._selected_objective] = updated
        if self._pending_edit_error_key:
            self._clear_pending_edit_error(self._pending_edit_error_key)
        self._error = ""
        self._invalidate_result()
        return True

    @Slot(str, "QVariant", result=bool)
    def setObjectiveField(self, field: str, value) -> bool:
        if self._busy or self._shutting_down:
            return False
        if not 0 <= self._selected_objective < len(self._objectives):
            return False
        if field in _OBJECTIVE_BOUNDS_FIELDS:
            return self.commitObjectiveBoundsDraft(field, str(value))
        if self._objective_bounds_pending():
            return False
        error_key = f"objective:{self._selected_objective}:{field}"
        if not self._pending_edit_allows(error_key):
            return False
        allowed = {
            "name", "wavelength_min_nm", "wavelength_max_nm", "wavelength_samples",
            "wavelength_step_nm", "angle_sampling_mode", "angle_min_deg", "angle_max_deg",
            "angle_step_deg", "angles_text", "polarization", "metric", "weight",
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
        updated = deepcopy(row)
        try:
            if field == "wavelength_samples":
                value = _integer(value, "波长采样点数", 1)
            elif field in ("polarization", "metric", "angle_sampling_mode"):
                value = str(value)
            elif field not in ("name", "angles_text"):
                value = _number(value, {
                    "wavelength_min_nm": "波长下限", "wavelength_max_nm": "波长上限",
                    "wavelength_step_nm": "采样步长",
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
        if row is None or (enabled and not row["available"]):
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
            if field == "upper_nm" and (value is None or str(value).strip() == ""):
                row[field] = None
            elif field == "lower_nm" and (value is None or str(value).strip() == ""):
                row[field] = 0.0
            else:
                row[field] = _edited_thickness_bound(
                    value, "厚度下限（nm）" if field == "lower_nm" else "厚度上限（nm）")
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

    @Slot(result=str)
    def openSimulationAndCalculate(self) -> str:
        """Simulate the current source session; never publish an optimizer candidate."""
        try:
            source = self._source_controller
            session = self.session
            if session is None:
                raise ValueError("没有有效的来源膜系；请从膜系设计重新打开优化窗口。")
            if (self._shutting_down or self._source_closed or source is None
                    or getattr(source, "session", None) is not session):
                raise ValueError("没有有效的来源膜系；请从膜系设计重新打开优化窗口。")
            if (self.project_context is not None
                    and getattr(source, "project_context", None) is not self.project_context):
                raise ValueError("来源膜系与当前优化工程不一致。")
            snapshot = cast(Any, session).snapshot()
            if getattr(snapshot, "error", "") or getattr(snapshot, "expression_busy", False):
                raise ValueError("当前来源膜系尚未就绪；请先修正输入或等待表达式完成。")
            cast(Any, self.service).validate(deepcopy(dict(snapshot.design)))
            identity = source.openSimulationAndCalculate()
            if not identity:
                raise ValueError("无法打开来源膜系的光谱仿真窗口。")
            return str(identity)
        except (ValueError, RuntimeError, TypeError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return ""

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
        self._sync_live_selection()
        self.changed.emit()
        self.plotDataChanged.emit()

    @Slot(int)
    def selectPlotAngle(self, index: int) -> None:
        angles = self.plotAngles
        if 0 <= int(index) < len(angles):
            self._plot_angle = float(angles[int(index)]["value"])
            self._sync_live_selection()
            self.plotDataChanged.emit()

    def _sync_live_selection(self):
        objectives = self._inspection_objectives or self._objectives
        if 0 <= self._plot_objective < len(objectives):
            self._live_display.set_selection(self._plot_objective, self._plot_angle,
                objectives[self._plot_objective]["metric"])

    def _build_request(self, *, require_variables: bool = True,
                       fixed_topology: bool = True,
                       prepared_angles: tuple[tuple[float, ...], ...] | None = None
                       ) -> FilmOptimizationRequest:
        if not self._commit_pending_angle_draft():
            raise ValueError(self._error or "请先完成角度输入")
        if self._objective_bounds_pending():
            objective_error = next(iter(self._objective_range_errors.values()), "")
            raise ValueError(objective_error or "Uncommitted target bounds; original text is retained")
        configuration_error = self._active_configuration_error(
            require_thickness_bounds=require_variables)
        if configuration_error:
            raise ValueError(configuration_error)
        snapshot = self.session.snapshot() if self.session is not None else None
        if snapshot is not None and snapshot.design_revision != self._source_revision:
            raise ValueError("原设计已修改；请先刷新设计快照")
        if prepared_angles is not None and len(prepared_angles) != len(self._objectives):
            raise ValueError("后台准备的角度网格与当前目标数不一致")
        variables = []
        for row in (() if self._search_mode == "synthesis" else self._variables):
            if not row["enabled"]:
                continue
            if not require_variables and row["upper_nm"] is None:
                continue
            if not row["available"]:
                raise ValueError(f"膜层 {row['layer_id']} 的厚度或光学数据不可用")
            if row["upper_nm"] is None and (fixed_topology and self._solver_id != "baseline"):
                raise ValueError("当前有限域厚度优化算法需要明确的厚度上界；不会自动添加人为上限")
            variables.append(FilmThicknessVariable(
                row["side"], row["layer_id"],
                _number(row["lower_nm"], "下限 nm"),
                (None if row["upper_nm"] is None else _number(row["upper_nm"], "上限 nm")),
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
            _validate_editor_criteria(row)
            objectives.append(FilmOptimizationObjective(
                wavelength_min_nm=low,
                wavelength_max_nm=high,
                wavelength_samples=samples,
                angles_deg=angles,
                polarization=row["polarization"],
                metric=row["metric"],
                pointwise=_constraint_from_editor(row["pointwise"]),
                mean=_constraint_from_editor(row["mean"]),
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
            execution_mode="deferred_batch" if fixed_topology and self._batch_execution_enabled() else "serial",
            execution_device=(self._selected_execution_binding()
                              if fixed_topology and self._batch_execution_enabled() else None),
        )

    def _build_reduction_request(self, *, prepared_angles=None):
        from coat_sim.core.film_reduction import FilmReductionRequest
        return FilmReductionRequest(self._build_request(prepared_angles=prepared_angles, fixed_topology=False), self._reduction_mode)

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

    def _synthesis_material_catalog(self) -> list[dict]:
        materials: dict[str, dict] = {}
        for row in self._structure_material_options:
            item = materials.setdefault(row["material_id"], {"id": row["material_id"],
                "name": row["material_name"], "optical_sets": []})
            item["optical_sets"].append({"id": row["optical_set_id"], "name": row["optical_set_name"]})
        return list(materials.values())

    def _synthesis_material_rows_error(self) -> str:
        if not self._synthesis_material_rows:
            return "请添加至少一行候选材料及数据集"
        available = {(row["material_id"], row["optical_set_id"]) for row in self._structure_material_options}
        seen = set()
        for index, row in enumerate(self._synthesis_material_rows):
            pair = (row["material_id"], row["optical_set_id"])
            if not all(pair):
                return f"候选材料第 {index + 1} 行：请选择材料及其数据集"
            if pair not in available:
                return f"候选材料第 {index + 1} 行已不可用：{pair[0]}/{pair[1]}；请重新选择或删除该行"
            if pair in seen:
                return f"候选材料第 {index + 1} 行重复：{pair[0]}/{pair[1]}"
            seen.add(pair)
        return ""

    @Property(list, notify=changed)
    def synthesisMaterialRows(self):
        materials = self._synthesis_material_catalog()
        by_id = {row["id"]: row for row in materials}
        options = [{"id": row["id"], "name": row["name"]} for row in materials]
        result = []
        for row in self._synthesis_material_rows:
            optical = by_id.get(row["material_id"], {}).get("optical_sets", [])
            error = ("材料或数据集已不可用，请重新选择或删除" if row["material_id"] and
                (row["material_id"] not in by_id or row["optical_set_id"] not in {item["id"] for item in optical})
                else "" if row["material_id"] and row["optical_set_id"] else "请选择材料及其数据集")
            result.append({**row, "material_options": deepcopy(options),
                           "optical_set_options": deepcopy(optical), "error": error})
        return result

    @Property(str, notify=changed)
    def synthesisMaterialError(self) -> str:
        return self._structure_material_error or self._synthesis_material_rows_error()

    def _synthesis_rows_changed(self) -> None:
        selected = {(row["material_id"], row["optical_set_id"]) for row in self._synthesis_material_rows}
        for row in self._structure_material_options:
            row["selected"] = (row["material_id"], row["optical_set_id"]) in selected
        self._synthesis_rows_initialized = True
        self._error = ""
        self._invalidate_result()

    @Slot(result=str)
    def addSynthesisMaterialRow(self) -> str:
        if self._busy or self._shutting_down or self._pending_edit_error:
            return ""
        identity = uuid4().hex
        self._synthesis_material_rows.append({"id": identity, "material_id": "", "optical_set_id": ""})
        self._synthesis_rows_changed()
        return identity

    @Slot(str, result=bool)
    def removeSynthesisMaterialRow(self, row_id: str) -> bool:
        if self._busy or self._shutting_down or self._pending_edit_error:
            return False
        row = next((item for item in self._synthesis_material_rows if item["id"] == row_id), None)
        if row is None:
            return False
        self._synthesis_material_rows.remove(row)
        self._synthesis_rows_changed()
        return True

    @Slot(str, int, result=bool)
    def moveSynthesisMaterialRow(self, row_id: str, delta: int) -> bool:
        if self._busy or self._shutting_down or self._pending_edit_error or delta not in (-1, 1):
            return False
        index = next((i for i, row in enumerate(self._synthesis_material_rows) if row["id"] == row_id), -1)
        target = index + delta
        if index < 0 or not 0 <= target < len(self._synthesis_material_rows):
            return False
        self._synthesis_material_rows.insert(target, self._synthesis_material_rows.pop(index))
        self._synthesis_rows_changed()
        return True

    @Slot(str, str, result=bool)
    def setSynthesisRowMaterial(self, row_id: str, material_id: str) -> bool:
        if self._busy or self._shutting_down or self._pending_edit_error:
            return False
        row = next((item for item in self._synthesis_material_rows if item["id"] == row_id), None)
        material = next((item for item in self._synthesis_material_catalog() if item["id"] == material_id), None)
        if row is None or material is None:
            return False
        row["material_id"] = material_id
        row["optical_set_id"] = material["optical_sets"][0]["id"]
        self._synthesis_rows_changed()
        return True

    @Slot(str, str, result=bool)
    def setSynthesisRowOpticalSet(self, row_id: str, optical_set_id: str) -> bool:
        if self._busy or self._shutting_down or self._pending_edit_error:
            return False
        row = next((item for item in self._synthesis_material_rows if item["id"] == row_id), None)
        if row is None or not any(item["material_id"] == row["material_id"] and item["optical_set_id"] == optical_set_id
                                  for item in self._structure_material_options):
            return False
        row["optical_set_id"] = optical_set_id
        self._synthesis_rows_changed()
        return True

    @Property(int, notify=changed)
    def synthesisMaterialCatalogRevision(self) -> int:
        return self._synthesis_material_catalog_revision

    @Slot(result=bool)
    def refreshSynthesisMaterials(self) -> bool:
        if self._busy or self._shutting_down or self._search_mode != "synthesis" or not self._project_current():
            return False
        before, error = deepcopy(self._structure_material_options), self._structure_material_error
        refresher = getattr(self.service, "refresh_materials", None)
        try:
            if callable(refresher):
                refresher()
            self._refresh_structure_material_options(preserve_selection=True)
        except (ValueError, OSError) as exc:
            self._structure_material_error = str(exc)
        if before != self._structure_material_options or error != self._structure_material_error:
            self._synthesis_material_catalog_revision += 1
            self.changed.emit()
        return not self._structure_material_error

    def _build_synthesis_request(self, *, prepared_angles=None):
        synthesis_structure_search_seed(self._synthesis_structure_search_seed_text)
        progress_policy = self._synthesis_progress_draft.policy_payload()
        from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
        base = self._build_request(require_variables=False, fixed_topology=False,
                                   prepared_angles=prepared_angles)
        error = self._synthesis_material_rows_error()
        if error:
            raise ValueError(error)
        refs = tuple(FilmNeedleMaterialReference(r["material_id"], r["optical_set_id"])
                     for r in self._synthesis_material_rows)
        return build_synthesis_request(base, refs,
                                       {**self._synthesis_options, "objective_progress_policy": progress_policy},
                                       self._synthesis_use_current_seed,
                                       self._synthesis_assume_unknown_k_zero,
                                       cpu_workers=self._synthesis_cpu_workers,
                                       forward_backend=self._synthesis_forward_backend,
                                       forward_device=self._synthesis_forward_device)

    def _synthesis_configuration_error(self):
        try:
            self._synthesis_progress_draft.policy_payload()
        except ValueError as exc:
            return str(exc)
        if (self._synthesis_progress_draft.enabled
                and self._synthesis_options.get("search_strategy", "hybrid") != "hybrid"):
            return "目标进展交接仅支持统一混合搜索；请关闭该实验开关或切回统一混合。"
        if self._synthesis_structure_search_seed_error:
            return self._synthesis_structure_search_seed_error
        if self._pending_edit_error:
            return self._error or "请完成当前输入"
        if self._structure_material_error:
            return self._structure_material_error
        material_error = self._synthesis_material_rows_error()
        if material_error:
            return material_error
        if self._design.get("substrate", {}).get("sides") != "single" or self._design.get("first_surface") != "front":
            return "从零合成目前仅支持单面、前表面入射"
        if not self._design.get("use_k"):
            return "从零合成要求保留真实 n/k，不能关闭 k"
        if self._validation_samples_multiplier < 2:
            return "自动合成的独立审计倍率至少为 2"
        try:
            synthesis_options(self._synthesis_options)
            synthesis_cpu_workers(self._synthesis_cpu_workers)
            from coat_sim.core.film_synthesis_forward import validate_forward_device
            validate_forward_device(self._synthesis_forward_device, self._synthesis_forward_backend)
        except (ValueError, TypeError, OverflowError) as exc:
            return str(exc)
        if self._synthesis_forward_backend != "cpu":
            if self._synthesis_forward_probe.busy:
                return "正在检查所选 GPU 前向设备，请等待探测完成。"
            result = self._synthesis_forward_probe.results.get(self._synthesis_forward_backend, {})
            if not result.get("available"):
                reason = result.get("reason") or "尚未完成设备探测"
                return f"所选 {self._synthesis_forward_backend.upper()} 前向不可用：{reason}"
            if self._synthesis_forward_device not in result.get("devices", []):
                return "所选 GPU 设备身份与当前解释器的探测结果不一致，请重新选择设备。"
        return ""

    @Property(dict, notify=changed)
    def synthesisOptions(self):
        return dict(self._synthesis_options)

    @Property(bool, notify=changed)
    def synthesisProgressPolicyEnabled(self) -> bool:
        return self._synthesis_progress_draft.enabled

    @Property(dict, notify=changed)
    def synthesisProgressPolicyDrafts(self) -> dict:
        return dict(self._synthesis_progress_draft.texts)

    @Property(str, notify=changed)
    def synthesisProgressPolicyError(self) -> str:
        return self._synthesis_progress_error

    def _invalidate_progress_draft(self, *, persist: bool) -> None:
        # Keystrokes invalidate a previous attempt but do not run validation or
        # try to persist an unfinished enabled policy as its last valid values.
        loading = self._configuration_loading
        if not loading:
            self._configuration_load_error = ""
        self._configuration_loading = loading or not persist
        try:
            self._invalidate_result()
        finally:
            self._configuration_loading = loading

    @Slot(bool, result=bool)
    def setSynthesisProgressPolicyEnabled(self, enabled: bool) -> bool:
        if self.busy or self._shutting_down or type(enabled) is not bool:
            return False
        self._synthesis_progress_draft.enabled = enabled
        self._synthesis_progress_edit_dirty = True
        if self._error == self._synthesis_progress_error:
            self._error = ""
        self._synthesis_progress_error = ""
        # Enabling never inserts a threshold. Blank fields keep start disabled.
        try:
            self._synthesis_options["objective_progress_policy"] = self._synthesis_progress_draft.policy_payload()
            valid = True
        except ValueError:
            self._synthesis_options["objective_progress_policy"] = None
            valid = False
        self._invalidate_progress_draft(persist=valid)
        if valid:
            self._synthesis_progress_edit_dirty = False
        return True

    @Slot(str, str, result=bool)
    def setSynthesisProgressPolicyText(self, name: str, text: str) -> bool:
        if (self.busy or self._shutting_down or not self._synthesis_progress_draft.enabled
                or name not in PROGRESS_FIELD_LABELS):
            return False
        self._synthesis_progress_draft.texts[name] = text
        self._synthesis_progress_edit_dirty = True
        self._synthesis_options["objective_progress_policy"] = None
        self._invalidate_progress_draft(persist=False)
        return True

    @Slot(result=bool)
    def finishSynthesisProgressPolicyEdit(self) -> bool:
        if self.busy or self._shutting_down:
            return False
        if not self._synthesis_progress_edit_dirty and not self._synthesis_progress_error:
            return True
        try:
            policy = self._synthesis_progress_draft.policy_payload()
        except ValueError as exc:
            self._synthesis_progress_error = str(exc)
            self._error = str(exc)
            self.changed.emit()
            return False
        self._synthesis_options["objective_progress_policy"] = policy
        self._synthesis_progress_error = self._error = ""
        self._synthesis_progress_edit_dirty = False
        self._configuration_load_error = ""
        self._persist_configuration()
        self.changed.emit()
        return True

    @Property(str, notify=changed)
    def synthesisStructureSearchSeedText(self) -> str:
        return self._synthesis_structure_search_seed_text

    @Property(str, notify=changed)
    def synthesisStructureSearchSeedError(self) -> str:
        return self._synthesis_structure_search_seed_error

    @Property(str, notify=changed)
    def synthesisActualStructureSearchSeedText(self) -> str:
        seed = self._synthesis_actual_structure_search_seed
        return "" if seed is None else str(seed)

    @Slot(str, result=bool)
    def setSynthesisStructureSearchSeedText(self, text: str) -> bool:
        if (self._busy or self._shutting_down
                or not self._synthesis_options.get("randomize_template_search", False)):
            return False
        self._synthesis_structure_search_seed_text = text
        try:
            seed = synthesis_structure_search_seed(text)
        except ValueError as exc:
            self._synthesis_structure_search_seed_error = str(exc)
            self._invalidate_result()
            return False
        self._synthesis_structure_search_seed_error = ""
        self._synthesis_options["structure_search_seed"] = seed
        self._error = ""
        self._invalidate_result()
        return True

    @Slot(result=bool)
    def finishSynthesisStructureSearchSeedEdit(self) -> bool:
        if self._synthesis_structure_search_seed_error:
            self._error = self._synthesis_structure_search_seed_error
            self.changed.emit()
            return False
        return True

    @Property(int, notify=changed)
    def synthesisCpuWorkers(self) -> int:
        return self._synthesis_cpu_workers

    @Property(str, notify=changed)
    def synthesisForwardBackend(self) -> str:
        return self._synthesis_forward_backend

    @Property(cast(type, "QVariantMap"), notify=changed)
    def synthesisForwardDevice(self) -> dict[str, Any]:
        return deepcopy(self._synthesis_forward_device or {})

    @Property(cast(type, "QVariantList"), notify=changed)
    def synthesisForwardBackendOptions(self) -> list[dict[str, Any]]:
        options = [{"id": "cpu", "label": "CPU（默认）", "available": True}]
        results = self._synthesis_forward_probe.results
        for backend in ("cuda", "rocm", "xpu"):
            result = results.get(backend, {})
            if result.get("available"):
                options.append({"id": backend, "label": backend.upper(), "available": True})
        if self._synthesis_forward_backend != "cpu" and not any(
                row["id"] == self._synthesis_forward_backend for row in options):
            options.append({"id": self._synthesis_forward_backend,
                            "label": f"{self._synthesis_forward_backend.upper()}（不可用）",
                            "available": False})
        return options

    @Property(cast(type, "QVariantList"), notify=changed)
    def synthesisForwardDeviceOptions(self) -> list[dict[str, Any]]:
        if self._synthesis_forward_backend == "cpu":
            return []
        result = self._synthesis_forward_probe.results.get(self._synthesis_forward_backend, {})
        rows = []
        for binding in result.get("devices", []):
            rows.append({"index": binding["index"], "binding": deepcopy(binding),
                         "label": f"{binding['name']} · {binding['runtime']}:{binding['index']}"
                                  + (f" · {binding['uuid']}" if binding.get("uuid") else "")})
        return rows

    @Property(str, notify=changed)
    def synthesisForwardStatus(self) -> str:
        status = self._synthesis_forward_probe.status
        results = self._synthesis_forward_probe.results
        unavailable = [f"{backend.upper()}：{row.get('reason') or '不可用'}"
                       for backend, row in results.items() if not row.get("available")]
        if unavailable and not self._synthesis_forward_probe.busy:
            status += "。" + "；".join(unavailable)
        return status

    @Property(bool, notify=changed)
    def synthesisForwardProbeBusy(self) -> bool:
        return self._synthesis_forward_probe.busy

    @Slot()
    def probeSynthesisForwardDevices(self) -> None:
        if self._busy or self._shutting_down:
            return
        self._synthesis_forward_probe.probe()

    @Slot(str, result=bool)
    def setSynthesisForwardBackend(self, backend: str) -> bool:
        if self._busy or self._shutting_down or self._pending_edit_error:
            return False
        backend = str(backend).lower()
        if backend not in {"cpu", "cuda", "rocm", "xpu"}:
            self._error = "未知的膜系合成前向设备。"
            self.changed.emit()
            return False
        binding = None
        if backend != "cpu":
            result = self._synthesis_forward_probe.results.get(backend, {})
            devices = result.get("devices", [])
            if not result.get("available") or not devices:
                self._error = f"{backend.upper()} 前向当前不可用：{result.get('reason') or '请先探测设备'}"
                self.changed.emit()
                return False
            current = self._synthesis_forward_device
            binding = current if current in devices else devices[0]
        if backend == self._synthesis_forward_backend and binding == self._synthesis_forward_device:
            return True
        self._synthesis_forward_backend = backend
        self._synthesis_forward_device = deepcopy(binding)
        self._error = ""
        self._invalidate_result()
        return True

    @Slot(int, result=bool)
    def setSynthesisForwardDevice(self, index: int) -> bool:
        if self._busy or self._shutting_down or self._pending_edit_error:
            return False
        result = self._synthesis_forward_probe.results.get(self._synthesis_forward_backend, {})
        devices = result.get("devices", [])
        if (isinstance(index, bool) or index < 0 or not isinstance(devices, list)
                or index >= len(devices)):
            self._error = "所选 GPU 设备不存在，请重新探测。"
            self.changed.emit()
            return False
        binding = devices[index]
        if binding != self._synthesis_forward_device:
            self._synthesis_forward_device = deepcopy(binding)
            self._error = ""
            self._invalidate_result()
        return True

    @Slot(int, object)
    def _on_synthesis_forward_probe_finished(self, _generation: int, _results: object) -> None:
        self.changed.emit()

    @Slot("QVariant", result=bool)
    def setSynthesisCpuWorkers(self, value: Any) -> bool:
        error_key = "synthesis:cpu_workers"
        if self._busy or self._shutting_down or not self._pending_edit_allows(error_key):
            return False
        try:
            workers = synthesis_cpu_workers(value)
        except (ValueError, TypeError, OverflowError) as exc:
            self._record_pending_edit_error(error_key, str(exc))
            return False
        self._clear_pending_edit_error(error_key)
        self._synthesis_cpu_workers = workers
        self._error = ""
        self._invalidate_result()
        return True

    @Property(bool, notify=changed)
    def canEditSynthesisAssumption(self) -> bool:
        return not (self._busy or self._shutting_down or self._pending_edit_error)

    @Property(bool, notify=changed)
    def synthesisAssumeUnknownKZero(self) -> bool:
        return self._synthesis_assume_unknown_k_zero

    @Slot(bool, result=bool)
    def setSynthesisAssumeUnknownKZero(self, enabled: bool) -> bool:
        if self._busy or self._shutting_down or self._pending_edit_error:
            return False
        self._synthesis_assume_unknown_k_zero = bool(enabled)
        self._error = ""
        self._invalidate_result()
        return True

    @Property(bool, notify=changed)
    def synthesisUseCurrentSeed(self):
        return self._synthesis_use_current_seed

    @Slot(bool, result=bool)
    def setSynthesisUseCurrentSeed(self, enabled):
        if self._busy or self._pending_edit_error:
            return False
        self._synthesis_use_current_seed = bool(enabled)
        self._invalidate_result()
        return True

    @Slot(str, "QVariant", result=bool)
    def setSynthesisOption(self, name, value):
        if self._busy or self._shutting_down or not self._pending_edit_allows("synthesis:" + name):
            return False
        try:
            values = synthesis_options({**self._synthesis_options, name: value})
        except (ValueError, TypeError, OverflowError) as exc:
            self._record_pending_edit_error("synthesis:" + name, str(exc))
            return False
        self._clear_pending_edit_error("synthesis:" + name)
        self._synthesis_options = values
        if name == "objective_progress_policy":
            self._synthesis_progress_draft = ObjectiveProgressDraft.from_configuration(None, values[name])
            self._synthesis_progress_error = ""
            self._synthesis_progress_edit_dirty = False
        if name == "structure_search_seed":
            seed = values[name]
            self._synthesis_structure_search_seed_text = "" if seed is None else str(seed)
            self._synthesis_structure_search_seed_error = ""
        self._error = ""
        self._invalidate_result()
        return True

    @Property(dict, notify=changed)
    def synthesisCandidateAudit(self):
        return deepcopy((self._synthesis_payload or {}).get("best_candidate_audit", {}))

    @Slot(bool, result=bool)
    def setSynthesisStrictUpperBound(self, enabled: bool) -> bool:
        """Select < only for this synthesis objective, never change its bound."""
        if (self._busy or self._shutting_down or self._search_mode != "synthesis"
                or self._pending_edit_error or self._objective_bounds_pending()
                or not 0 <= self._selected_objective < len(self._objectives)
                or type(enabled) is not bool):
            return False
        row = self._objectives[self._selected_objective]["pointwise"]
        if enabled and not row["enabled"]:
            return False
        row["strict_upper"] = enabled
        self._error = ""
        self._invalidate_result()
        return True

    @staticmethod
    def _synthesis_acceptance_text(value) -> str:
        return "通过" if value is True else "未通过" if value is False else "未知（缺少完整核查）"

    @Property(str, notify=changed)
    def synthesisExecutionSummary(self) -> str:
        if self._synthesis_payload is None:
            return ""
        payload = self._synthesis_payload
        result = payload.get("result", {})
        performance = result.get("performance") if isinstance(result, dict) else None
        if not isinstance(performance, dict):
            return "未记录合成执行诊断"
        forward = performance.get("forward")
        if not isinstance(forward, dict):
            forward = {}
        forward_requested = forward.get("requested_backend")
        forward_actual = forward.get("actual_backend")
        forward_device = forward.get("device_identity")
        forward_text = ""
        if isinstance(forward_requested, str):
            forward_label = {"cpu": "CPU", "cuda": "CUDA", "rocm": "ROCm", "xpu": "XPU"}.get(
                forward_requested, forward_requested.upper())
            actual_label = ({"cpu": "CPU", "cuda": "CUDA", "rocm": "ROCm", "xpu": "XPU",
                             "gpu": "GPU"}.get(forward_actual, "未记录")
                            if isinstance(forward_actual, str) else "未记录")
            device_label = ""
            if isinstance(forward_device, dict):
                runtime = forward_device.get("runtime")
                index = forward_device.get("index")
                name = forward_device.get("name")
                if isinstance(runtime, str) and type(index) is int:
                    device_label = f"（{runtime}:{index}" + (f" {name}" if isinstance(name, str) else "") + "）"
            forward_text = (f"前向请求 {forward_label}，实际 {actual_label}{device_label}；"
                            f"批次/候选提交完成采用：{forward.get('batches', '未知')}/"
                            f"{forward.get('candidates_submitted', '未知')}/"
                            f"{forward.get('candidates_completed', '未知')}/"
                            f"{forward.get('candidates_consumed', '未知')}；")
        backend = performance.get("backend", "unknown")
        requested = performance.get("requested_workers", "unknown")
        effective = performance.get("effective_workers", "unknown")
        pids = performance.get("worker_pids")
        dispatched = performance.get("workers_with_submitted_tasks")
        owned = performance.get("owned_worker_count")
        if (type(dispatched) is int and dispatched >= 0 and
                (backend == "headless_process" or type(owned) is int and owned > 0)):
            # Owned/ready handshake PIDs may include idle or startup-cancelled
            # workers; only actually dispatched workers count as working.
            effective = dispatched
        process_count = effective if isinstance(effective, int) and not isinstance(effective, bool) else "未知"
        pid_count = len(pids) if isinstance(pids, list) else "未知"
        request_text = "自动" if requested == 0 else str(requested)
        backend_text = {"serial": "CPU 串行", "headless_process": "CPU 多进程"}.get(
            backend, "CPU 执行方式未知")
        historical = performance.get("historical") is True
        history_text = "历史检查点记录" if historical else "本次执行记录"

        def count(name: str) -> str:
            value = performance.get(name)
            return str(value) if type(value) is int and value >= 0 else "未知"

        fallback_events = performance.get("fallback_events")
        reasons = []
        if isinstance(fallback_events, list):
            for item in fallback_events[:3]:
                if isinstance(item, str) and item:
                    reasons.append(item)
                elif isinstance(item, dict) and (item.get("reason") or item.get("code")):
                    reasons.append(str(item.get("reason") or item.get("code")))
        fallback_reason = "、".join(reasons) if reasons else "未记录"
        batch_text = ""
        search = performance.get("search")
        hybrid = search.get("hybrid") if isinstance(search, dict) else None
        if isinstance(hybrid, dict):
            quantum = hybrid.get("frozen_parallel_batch_size")
            logical = str(quantum) if type(quantum) is int and quantum > 0 else "未知"
            records = hybrid.get("parallel_batches")
            records = records if isinstance(records, (tuple, list)) else ()
            caps = sorted({row["resource_worker_cap"] for row in records if isinstance(row, dict)
                and type(row.get("resource_worker_cap")) is int and row["resource_worker_cap"] > 0})
            cap_text = "/".join(map(str, caps)) if caps else "未记录"
            batch_text = (f"冻结逻辑批次：{logical}；阶段准入并发上限：{cap_text}；"
                "可并行：独立候选/解析针扫；串行：联合厚度精修/采纳；")
        return (forward_text + batch_text + f"{backend_text}（{history_text}；PID 仅为记录，不代表进程仍在运行）；"
                f"请求进程数：{request_text}；实际工作进程数：{process_count}；记录的 PID 数：{pid_count}；"
                f"任务提交/完成：{count('submitted_tasks')}/{count('completed_tasks')}；"
                f"按原序采用：{count('consumed_tasks')}；投机未采用：{count('discarded_tasks')}；"
                f"串行回退：{count('serial_fallback_tasks')}（原因：{fallback_reason}）")

    @Property(list, notify=changed)
    def synthesisLayers(self):
        rows = deepcopy((self._synthesis_payload or {}).get("review", {}).get("layers", []))
        for row in rows:
            source = row.get("material_source", {})
            row["display"] = (f"{row.get('index', 0) + 1} | {row.get('id')} | "
                f"{row.get('material_name')} ({row.get('material_id')}) / "
                f"{row.get('optical_set_name')} ({row.get('optical_set_id')}) | {format_film_thickness(row.get('thickness_nm'))} nm\n"
                f"n 波段 {row.get('n_wavelength_domain_nm')} nm，k 波段 {row.get('k_wavelength_domain_nm')} nm\n"
                + json.dumps(source, ensure_ascii=False, sort_keys=True))
        return rows

    @Property(list, notify=changed)
    def synthesisObjectiveAudit(self):
        rows = deepcopy((self._synthesis_payload or {}).get("review", {}).get("per_objective", []))
        for row in rows:
            index = row["objective_index"]
            quality = (f"原约束验收：{self._synthesis_acceptance_text(row.get('targets_met'))}；"
                       f"求解容差：{self._synthesis_acceptance_text(row.get('solver_acceptable'))}")
            if row.get("score_scope") == "training":
                quality += "\n仅训练网格；未完成独立验证"
            row["display"] = (f"目标 {index + 1} | {row['metric']} | "
                f"{row['wavelength_min_nm']}–{row['wavelength_max_nm']} nm\n{quality}\n"
                + self._criterion_status_text(row))
        return rows

    @Property(str, notify=changed)
    def synthesisAbsorptionSummary(self) -> str:
        if self._synthesis_payload is None:
            return ""
        assumption = self._synthesis_payload.get("absorption_assumption")
        if not isinstance(assumption, dict):
            return "此历史结果未记录未知 k 策略，请查看原始任务输入。"
        if not assumption.get("assume_unknown_k_zero"):
            return "吸收策略：严格使用已知 k；未把未知吸收按 0 计算。"
        refs = assumption.get("affected_materials", [])
        identities = "、".join(f"{row['material_id']} / {row['optical_set_id']}" for row in refs)
        used = assumption.get("used_affected_materials", [])
        used_identities = "、".join(f"{row['material_id']} / {row['optical_set_id']}" for row in used)
        if not identities:
            return "已启用未知 k 按 0 假设；本任务未发现需要替代的未知 k 数据集。已知非零 k 保留。"
        candidate_scope = (f"当前候选实际采用此假设的数据集：{used_identities}。"
                           if used_identities else "当前候选未使用这些需要假设的数据集。")
        return ("吸收假设：仅将未知 k 按 0 计算，已知非零 k 保留。\n"
                f"受影响的输入数据集：{identities}\n{candidate_scope}\n"
                "审计基于所记录的输入策略，不是实际吸收的实验验证；原材料记录未改变。")

    @Property(str, notify=changed)
    def synthesisAuditSummary(self) -> str:
        return self._synthesis_audit_summary()

    def _synthesis_work_count(self, name: str) -> str:
        value = (self._synthesis_payload or {}).get("result", {}).get(name)
        return "未知" if value is None else str(value)

    def _synthesis_work_counters_note(self) -> str:
        payload = self._synthesis_payload or {}
        work = payload.get("work_counters", {})
        names = ("cycles", "evaluations", "failed_evaluations", "validation_evaluations",
                 "audit_physical_evaluations", "audit_wavelength_points")
        if (work.get("work_counters_complete") is not False
                and all(payload.get("result", {}).get(name) is not None for name in names)):
            return ""
        text = "\n历史检查点缺少完整累计工作量；未知项未按 0 报告。统计范围：原任务截至已保存检查点。"
        replay = work.get("recovery_replay_work")
        if isinstance(replay, dict):
            def count(name: str) -> str:
                value = replay.get(name)
                return "未知" if value is None else str(value)
            text += (f"\n本次恢复重放单列：训练评价 {count('training_evaluations')}、"
                     f"审计候选评价 {count('validation_evaluations')}、"
                     f"物理调用 {count('audit_physical_evaluations')}、波长点 {count('audit_wavelength_points')}。")
        return text

    def _synthesis_audit_summary(self) -> str:
        payload = self._synthesis_payload
        if payload is None:
            return ""
        raw, facts = payload["result"], payload["best_candidate_audit"]
        quality = self._synthesis_acceptance_text(facts.get("targets_met"))
        acceptable = self._synthesis_acceptance_text(facts.get("solver_acceptable"))
        layers = payload.get("review", {}).get("layers", [])
        return (f"任务状态：{raw['state']} / {raw['termination_reason']}\n"
                f"候选独立审计：原门限名义验收{quality}，层数 {len(layers)}\n"
                f"求解容差可接受：{acceptable}，最大归一化违规 {facts.get('audit_max_normalized_error')}，"
                f"求解容差 {facts.get('acceptable_normalized_error')}（不放宽原门限，不添加制造余量）\n"
                f"审计物理调用 {self._synthesis_work_count('audit_physical_evaluations')}，"
                f"审计波长点 {self._synthesis_work_count('audit_wavelength_points')}。"
                "有限采样审计，无连续谱证书。原设计仅在确认应用后改变。"
                + self._synthesis_work_counters_note()
                + ("\n" + "\n".join(str(warning) for warning in payload.get("review", {}).get("warnings", []))
                   if payload.get("review", {}).get("warnings") else ""))

    @Property(str, notify=changed)
    def synthesisPreviewLabel(self):
        if self.project_context is not None or not callable(getattr(self.parent(), "open_window", None)):
            return "查看候选采样光谱"
        return "打开候选采样预览"

    @Slot(result=bool)
    def previewSynthesisCandidate(self):
        if self._busy or self._result is None or self._synthesis_payload is None or self._frozen_request is None:
            return False
        if not self._project_current():
            self._error = "来源项目已关闭或重新打开，未打开旧候选预览"
            self.changed.emit()
            return False
        opener = getattr(self.parent(), "open_window", None)
        if self.project_context is not None or not callable(opener):
            self._error = ""
            self.synthesisPreviewRequested.emit()
            self.plotDataChanged.emit()
            self.changed.emit()
            return True
        try:
            payload = synthesis_preview_handoff(self._frozen_request, self._result,
                task_state=self._synthesis_payload["result"]["state"])
            return bool(opener("film_simulation", mode="new", payload=payload))
        except (ValueError, TypeError, KeyError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False

    def _continuation_materials_current(self, record: dict) -> bool:
        frozen = record["editable_snapshot"] or record["request"].frozen_material_snapshot
        materials = getattr(self.service, "materials", None)
        if materials is None:
            return False
        if not record["history"] and materials is not record["materials_owner"]:
            return False
        try:
            for material in (frozen or {}).get("materials", []):
                current = materials.get(material["id"])
                actual = {row["id"]: row for row in current["optical_sets"]}
                if any(current.get(key) != value for key, value in material.items()
                       if key != "optical_sets"):
                    return False
                for expected in material["optical_sets"]:
                    if expected != actual.get(expected["id"]):
                        return False
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
        return True

    def _continuation_source_reason(self, record: dict | None, *, admission=False) -> str:
        if record is None:
            return self._continuation_unavailable_reason
        if self._result_terminal_state == "提交状态未知":
            return "请先核对上次作业的未知提交状态，不能重复提交。"
        if (not admission and self.busy) or self._shutting_down:
            return "请先等待当前任务结束。"
        if self._source_closed or self._observers_detached or self._pending_close:
            return "来源设计窗口已关闭或正在关闭。"
        if self._pending_reuse_source is not None:
            return "正在切换来源设计，请先完成切换。"
        session = self.session
        if (session is None or record["session"] is not session
                or record["source_controller"] is not self._source_controller
                or getattr(self._source_controller, "session", None) is not self.session):
            return "来源设计会话已替换，不能继续旧候选。"
        if not self._project_current():
            return "来源项目已关闭或重新打开。"
        operation = record["project_operation"]
        if operation is not None and (self._project_compute is None
                or not self._project_compute.accepts(operation)):
            return "来源项目代次已改变。"
        try:
            from coat_sim.ui.film_optimization_continue_source import canonical_design_hash
            snapshot = session.snapshot()
            if (record.get("source_path") is not None
                    and str(snapshot.path or "") != record["source_path"]):
                return "来源设计资源已切换或另存，不能继续旧候选。"
            if snapshot.pending_confirmation or snapshot.expression_busy:
                return "来源设计有待确认操作或尚未完成的编辑。"
            epoch = self._source_edit_epoch()
            if epoch is None:
                return "无法核对来源未提交编辑状态。"
            if record["history"] and snapshot.dirty:
                return "历史继续要求已保存且无待提交编辑的来源设计。"
            if (snapshot.design_revision != record["allowed_revision"]
                    or epoch != record["allowed_epoch"]):
                return "来源设计、撤销状态或未提交编辑已改变。"
            if canonical_design_hash(snapshot.design) not in record["allowed_design_hashes"]:
                return "当前设计不再等于原来源或该次取消候选。"
            binding = record["source_binding"]
            identity = binding.get("managed_film") if isinstance(binding, dict) else None
            if identity is not None:
                resolver = getattr(self.service, "managed_film_identity", None)
                if not callable(resolver) or resolver(snapshot.path) != identity:
                    return "来源膜系资源或已保存版本已改变。"
            elif record["history"]:
                return "历史作业没有持久膜系身份，只可查看。"
        except (ValueError, KeyError, TypeError, OSError, AttributeError):
            return "无法核对来源膜系资源或已保存版本。"
        if not self._continuation_materials_current(record):
            return "原冻结材料闭包已改变，不能继续旧候选。"
        if record["mode"] == "synthesis" and record["editable_snapshot"] is None:
            return "历史合成缺少提交时的完整可编辑材料闭包，只可查看。"
        return ""

    @Property(bool, notify=changed)
    def canContinueOptimization(self) -> bool:
        return not self._continuation_source_reason(self._continuation_record)

    @Property(str, notify=changed)
    def continueDisabledReason(self) -> str:
        return self._continuation_source_reason(self._continuation_record)

    @Property(cast(type, "QVariantMap"), notify=changed)
    def continuationBudgetDefaults(self) -> dict:
        record = self._continuation_record
        if record is None:
            return {}
        request = record["request"]
        return {"mode": record["mode"],
                "max_iterations": getattr(request, "max_iterations", None),
                "max_cycles": getattr(request, "max_cycles", None),
                "max_evaluations": request.max_evaluations,
                "max_seconds": getattr(request, "max_seconds", None)}

    @Property(str, notify=changed)
    def previousSpentText(self) -> str:
        record = self._continuation_record
        if record is None:
            return ""
        result = record["result"]
        if record["mode"] == "synthesis":
            raw = record["synthesis_payload"]["result"]
            return (f"上次作业 {record['job_id']} 已耗：周期 {raw.get('cycles', '未知')}，"
                    f"评价 {raw.get('evaluations', '未知')}，时长 {raw.get('elapsed_seconds', '未知')} 秒。")
        return (f"上次作业 {record['job_id']} 已耗：迭代 {result.iterations}，评价 {result.evaluations}；"
                "时长未记录。")

    @Property(str, notify=changed)
    def priorRunFallbackText(self) -> str:
        record = self._prior_run_fallback
        if record is None:
            return ""
        return (f"前次保留结果（独立旧作业 {record['job_id']}）："
                f"最大归一化误差 {record['result'].max_normalized_error:.6g}。"
                "这是前次候选，不是新作业评分；在新作业首次完整评分前单独保留，不会再次自动应用。")

    def _retain_cancelled_restart(self) -> None:
        if self._attempt_search_mode in {"structure", "reduction"}:
            self._continuation_record = None
            self._continuation_unavailable_reason = "独立结构优化暂不支持从取消候选继续。"
            return
        if not self._cancel_candidate_verified():
            self._continuation_unavailable_reason = "取消时没有可用的完整评分候选。"
            return
        request = self._attempt_full_request or self._frozen_request
        if request is None or self.session is None:
            self._continuation_unavailable_reason = "只读仿真来源没有可核对的编辑会话。"
            return
        try:
            from coat_sim.ui.film_optimization_continue_source import build_source_binding
            binding = self._attempt_source_binding
            if binding is None:
                binding = build_source_binding(self.service, self.session.snapshot(), request,
                    editable_snapshot=self._synthesis_editable_snapshot)
            self._continuation_record = {
                "mode": self._attempt_search_mode, "request": request, "result": self._result,
                "synthesis_payload": deepcopy(self._synthesis_payload),
                "editable_snapshot": deepcopy(self._attempt_editable_snapshot or self._synthesis_editable_snapshot),
                "source_binding": deepcopy(binding), "job_id": self._attempt_job_id,
                "spec_fingerprint": self._attempt_spec_fingerprint,
                "session": self.session, "source_controller": self._source_controller,
                "materials_owner": self._attempt_materials_owner,
                "project_operation": self._attempt_project_operation,
                "allowed_revision": self._attempt_source_revision,
                "source_path": self._attempt_source_path,
                "allowed_epoch": self._attempt_edit_epoch,
                "allowed_design_hashes": {binding["source_design_sha256"]}, "history": False,
            }
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self._continuation_record = None
            self._continuation_unavailable_reason = "不能建立可信继续来源：" + str(exc)

    def _capture_continuation_postapply(self) -> None:
        record = self._continuation_record
        if (record is None or record["history"] or self.session is None
                or record["job_id"] != self._attempt_job_id):
            return
        from coat_sim.ui.film_optimization_continue_source import canonical_design_hash
        actual = self.session.snapshot()
        # Do not infer +1: applying an identical design intentionally has no edit.
        record["allowed_revision"] = actual.design_revision
        record["allowed_epoch"] = self._source_edit_epoch()
        record["allowed_design_hashes"] = {canonical_design_hash(actual.design)}
        record["materials_owner"] = getattr(self.service, "materials", None)

    def _bind_history_continuation(self, job_id, request, result, payload, source_context, mode) -> None:
        self._continuation_record = None
        self._prior_run_fallback = None
        self._continuation_unavailable_reason = "历史作业缺少可信来源绑定，只可查看。"
        session = self.session
        if session is None:
            return
        if mode == "structure":
            self._continuation_unavailable_reason = "独立结构优化暂不支持从取消候选继续。"
            return
        if not isinstance(result, FilmOptimizationResult) or result.state != "cancelled" or result.evaluations < 1:
            self._continuation_unavailable_reason = "只有保留完整评分候选的已取消作业可以继续。"
            return
        try:
            from coat_sim.ui.film_optimization_continue_source import canonical_design_hash, validate_source_binding
            if (not isinstance(source_context, dict) or source_context.get("source_job_id") != str(job_id)
                    or not isinstance(source_context.get("source_spec_fingerprint"), str)):
                return
            binding = validate_source_binding(source_context.get("binding"), request)
            if binding["managed_film"] is None:
                self._continuation_unavailable_reason = "匿名或未绑定来源的历史作业只可查看。"
                return
            editable = None
            if mode == "synthesis":
                from coat_sim.core.film_continuation_editable_snapshot import restore_editable_snapshot_delta
                editable = restore_editable_snapshot_delta(request.frozen_material_snapshot,
                    binding["synthesis_editable_delta"])
            snapshot = session.snapshot()
            self._continuation_record = {
                "mode": mode, "request": request, "result": result,
                "synthesis_payload": deepcopy(payload) if mode == "synthesis" else None,
                "editable_snapshot": editable, "source_binding": binding,
                "job_id": str(job_id), "spec_fingerprint": source_context["source_spec_fingerprint"],
                "session": self.session, "source_controller": self._source_controller,
                "materials_owner": getattr(self.service, "materials", None),
                "project_operation": self._project_operation,
                "allowed_revision": snapshot.design_revision, "allowed_epoch": self._source_edit_epoch(),
                "source_path": str(snapshot.path or ""),
                "allowed_design_hashes": {binding["source_design_sha256"], canonical_design_hash(result.best_design)},
                "history": True,
            }
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self._continuation_unavailable_reason = "历史继续来源不可用：" + str(exc)

    @Slot("QVariantMap", result=bool)
    def continueOptimization(self, budget: dict) -> bool:
        record = self._continuation_record
        reason = self._continuation_source_reason(record)
        session = self.session
        if reason or record is None or session is None:
            self._error = reason
            self.changed.emit()
            return False
        try:
            from coat_sim.core.film_optimization_continuation import (
                build_optimization_warm_restart, build_synthesis_warm_restart)
            values = dict(budget)
            revision = session.snapshot().design_revision
            if record["mode"] == "synthesis":
                if set(values) != {"max_cycles", "max_evaluations", "max_seconds"}:
                    raise ValueError("请明确设置新尝试的周期、评价和时长预算（允许留空）。")
                for key in ("max_cycles", "max_evaluations"):
                    if values[key] is not None:
                        values[key] = _integer(values[key], key)
                if values["max_seconds"] is not None:
                    values["max_seconds"] = _number(values["max_seconds"], "新时长预算")
                if all(value is None for value in values.values()):
                    raise ValueError("新尝试至少需要一项预算限制。")
                plan = build_synthesis_warm_restart(record["synthesis_payload"],
                    design_revision=revision, **values)
            else:
                if set(values) != {"max_iterations", "max_evaluations"}:
                    raise ValueError("请明确设置新尝试的迭代和评价预算。")
                values = {key: _integer(value, key) for key, value in values.items()}
                plan = build_optimization_warm_restart(record["request"], record["result"],
                    design_revision=revision, **values)
            plan.metadata.update(source_job_id=record["job_id"],
                source_spec_fingerprint=record["spec_fingerprint"])
            return self._start_optimization(warm_restart=(plan, record))
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._error = "未开始继续优化：" + str(exc)
            self.changed.emit()
            return False

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
                self._project_operation = None
                self._prepared_angle_job_id = None
                self._error = f"项目已关闭或正在退出；未准备角度网格：{exc}"
                self.changed.emit()
                return False
        self._pending_angle_preparation = {
            "operation_id": operation_id,
            "design_revision": revision,
            "edit_epoch": edit_epoch,
            "target_identity": self._compute_target.selection_identity(),
            "display_score_config": self._live_display.score_configuration(),
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
            self._project_operation = None
            self._prepared_angle_job_id = None
            raise
        self._error = ""
        self._progress = 0.0
        self._progress_text = "正在后台准备角度采样网格；尚未提交队列作业"
        self.changed.emit()
        return True

    def _start_optimization(self, *, prepared_start: _PreparedAngleGrid | None = None,
                            warm_restart: tuple[Any, dict] | None = None
                            ) -> bool:
        search_mode = warm_restart[1]["mode"] if warm_restart is not None else self._search_mode
        if warm_restart is not None:
            reason = self._continuation_source_reason(warm_restart[1])
            if reason:
                self._error = reason
                self.changed.emit()
                return False
        if self._initial_loading or self._initial_load_failed or self._structure_material_error:
            return False
        if self._result_terminal_state == "提交状态未知" and self._queue_job_id:
            self._error = f"请先核对作业 {self._queue_job_id} 的提交状态；未重复提交。"
            self.changed.emit()
            return False
        if self._pending_reuse_source is not None:
            self._pending_reuse_start = True
            return True
        if self.busy or self._shutting_down:
            return False
        if self._source_closed:
            self._error = "原设计窗口已关闭；不能开始或回写优化。"
            self.changed.emit()
            return False
        if prepared_start is None and warm_restart is None:
            try:
                configuration_error = self._active_configuration_error()
                if configuration_error:
                    raise ValueError(configuration_error)
                if self._requires_async_angle_preparation():
                    placeholders = tuple((0.0,) for _ in self._objectives)
                    request_template = (
                        self._build_synthesis_request(prepared_angles=placeholders)
                        if search_mode == "synthesis" else
                        self._build_structure_request(prepared_angles=placeholders)
                        if search_mode == "structure" else
                        self._build_reduction_request(prepared_angles=placeholders)
                        if search_mode == "reduction"
                        else self._build_request(prepared_angles=placeholders))
                    return self._start_angle_preparation(request_template)
            except Exception as exc:
                self._error = str(exc)
                self.changed.emit()
                return False
        try:
            if warm_restart is not None:
                request = warm_restart[0].request
            elif prepared_start is not None:
                request = prepared_start.request
            elif search_mode == "synthesis":
                request = self._build_synthesis_request()
            elif search_mode == "structure":
                request = self._build_structure_request()
            elif search_mode == "reduction":
                request = self._build_reduction_request()
            else:
                request = self._build_request()
        except (TypeError, ValueError, OverflowError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        try:
            store, _device = self._compute_target.client_for_submit(
                client_factory=SchedulerClient)
            remote = bool(getattr(getattr(store, "transport", None), "remote_http", False))
            if (remote and search_mode == "synthesis"
                    and getattr(request, "forward_backend", "cpu") != "cpu"):
                raise _QueueCompatibilityError(
                    "GPU 前向仅支持本机合成；请切回 CPU 前向后再选择远程任务，尚未提交作业。")
            if getattr(request,"execution_device",None) is not None:
                if request.execution_device != self._selected_execution_binding():
                    raise _QueueCompatibilityError("准备期间执行设备已改变；请重新开始，未提交任务。")
            verify_admission, target_is_current = self._compute_target.admission_verifier(store, _device)
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
            # Freeze inputs on the owner thread before background admission.
            # The worker still verifies the running queue before submission.
            editable_snapshot = (deepcopy(warm_restart[1]["editable_snapshot"]) if warm_restart is not None else
                prepared_start.editable_snapshot if prepared_start is not None else None)
            if prepared_start is None and warm_restart is None:
                if search_mode == "synthesis":
                    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
                    request = freeze_film_synthesis_request(self.service, request)
                    from coat_sim.core.film_physical_generator import freeze_synthesis_editable_snapshot
                    editable_snapshot = freeze_synthesis_editable_snapshot(
                        self.service, request.environment, request.frozen_material_snapshot)
                elif search_mode == "reduction":
                    from coat_sim.core.film_reduction import freeze_film_reduction_request
                    request = freeze_film_reduction_request(self.service, request)
                elif search_mode == "structure":
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
        attempt_project_operation: OperationBinding | None = None
        try:
            spec = (prepared_start.spec if prepared_start is not None else
                    synthesis_spec(self.service, request) if search_mode == "synthesis" else
                    freeze_spec(self.service, "film_optimization", {"request": request}))
            from coat_sim.scheduler.jobs import JobSpec
            from coat_sim.ui.film_optimization_continue_source import SOURCE_INPUT_KEY, build_source_binding
            source_binding = None
            if search_mode not in {"structure", "reduction"}:
                source_snapshot = self.session.snapshot() if self.session is not None else self._snapshot_source
                source_binding = build_source_binding(self.service, source_snapshot, request,
                    editable_snapshot=editable_snapshot)
                spec = deepcopy(spec)
                spec["inputs"][SOURCE_INPUT_KEY] = source_binding
            if warm_restart is not None:
                spec["inputs"]["film_warm_restart"] = deepcopy(warm_restart[0].metadata)
            spec_fingerprint = JobSpec.parse(spec).fingerprint
            prepared_angle_job_id = self._prepared_angle_job_id
            job_id = prepared_angle_job_id or uuid4().hex
            self._prepared_angle_job_id = None
            if self._project_compute is not None:
                if self._project_compute.editing_frozen:
                    raise ValueError("项目已关闭或正在退出；未提交优化任务。")
                if prepared_angle_job_id is None:
                    self._project_operation = self._project_compute.capture(job_id)
                    attempt_project_operation = self._project_operation
                else:
                    attempt_project_operation = self._project_operation
                    if not self._project_current():
                        raise ValueError("项目已关闭或正在退出；未提交优化任务。")
            base_request = (synthesis_base_request(request) if search_mode == "synthesis" else
                            cast(FilmOptimizationRequest, getattr(request, "base_request", request)))
            metadata = {
                "summary": ("Film automatic synthesis | "
                            if search_mode == "synthesis" else
                            "Film reduction | " if search_mode == "reduction" else
                            "Film structure synthesis | " if search_mode == "structure"
                            else "Film optimization | ") + str(
                    base_request.design_snapshot.get("name", "film design")),
                "origin": {"surface": "desktop"},
                "input_summary": {
                    "design_name": str(base_request.design_snapshot.get("name", "")),
                    "design_revision": base_request.design_revision,
                    "objective_count": len(base_request.objectives),
                    "variable_count": len(base_request.variables),
                    "search_mode": search_mode,
                    "structure_mode": (request.mode if search_mode == "structure" else None),
                    "reduction_mode": (request.mode if search_mode == "reduction" else None),
                    "candidate_material_count": (len(request.candidate_materials)
                                                  if search_mode in {"structure", "synthesis"} else 0),
                },
            }
            if self._project_compute is not None:
                if self._project_operation is None:
                    raise ValueError("Project optimization is missing its frozen operation")
                metadata = self._project_compute.metadata(self._project_operation, metadata)
            cancel_event = (_LocalAdmissionCancelEvent(store, job_id)
                            if not remote else
                            RemoteCancelEvent(store, job_id))
            cancel_event.on_cancel_status = self._cancelTransportStatus.emit
            admission_project = self._project_compute
            admission_operation = self._project_operation
            admission_stopped = self._admission_stopped

            original_verify_admission = verify_admission
            synthesis_mode = search_mode == "synthesis"
            admission_revision = (base_request.design_revision if warm_restart is not None
                                  else self._source_revision)
            admission_epoch = self._source_edit_epoch()
            admission_session = self.session
            def verify_admission():
                if warm_restart is not None:
                    reason = self._continuation_source_reason(warm_restart[1], admission=True)
                    if reason:
                        raise InterruptedError("继续作业提交前来源已改变：" + reason)
                if synthesis_mode:
                    from coat_sim.ui.compute_target import validate_capabilities
                    validate_capabilities(store.capabilities(), "film_synthesis", remote=remote)
                    if (admission_session is not None and (
                            admission_session.snapshot().design_revision != admission_revision
                            or self._source_edit_epoch() != admission_epoch)):
                        raise InterruptedError("合成提交前原设计或未提交编辑已改变")
                original_verify_admission()

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
                    verify_admission()
                    target_is_current()
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
                            capabilities, "film_synthesis" if synthesis_mode else "film_optimization", remote=True)
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
                    target_is_current()
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
            if not remote:
                local_cancel_event = cast(_LocalAdmissionCancelEvent, cancel_event)

                def async_local_admission():
                    verify_admission()
                    if local_cancel_event.cancel_intent.is_set():
                        raise InterruptedError(
                            "已在本地作业接纳前取消；未创建作业。")
                    if not admission_project_is_current():
                        raise InterruptedError("项目已关闭或正在退出；未接纳新的优化作业。")
                    target_is_current()
                    from coat_sim.scheduler.desktop import submit_local_with_receipt
                    submit_local_with_receipt(store, spec, job_id=job_id, metadata=metadata)
                    local_cancel_event.mark_admitted()

            self._sync_live_selection()
            display_attempt = ("optimization", self._operation_serial + 1)
            display_revision, display_epoch = admission_revision, self._source_edit_epoch()
            display_session, display_source = self.session, self._source_controller
            metadata["film_display_config"] = self._live_display.begin(display_attempt,
                objectives=base_request.objectives,
                score_config=(prepared_start.display_score_config if prepared_start is not None else None),
                source_current=lambda: (not self._source_closed and not self._shutting_down
                    and self._operation_serial == display_attempt[1] and self._project_current()
                    and not self._observers_detached
                    and self.session is display_session and self._source_controller is display_source
                    and (self.session is None or self.session.snapshot().design_revision == display_revision)
                    and self._source_edit_epoch() == display_epoch))

            def create_worker(operation_id):
                args: tuple[Any, FilmOptimizationRequest, Any, str, RemoteCancelEvent, int] = (
                    self.service, base_request, store, job_id, cancel_event, operation_id)
                queue_admission = remote_admission
                if queue_admission is None:
                    queue_admission = async_local_admission
                task_class = _FilmSynthesisTask if search_mode == "synthesis" else _FilmOptimizationTask
                task = (task_class(*args, remote_admission=queue_admission,
                                   project_compute=self._project_compute)
                        if queue_admission is not None else
                        task_class(*args, project_compute=self._project_compute))
                def bind_display(client, admitted_job_id):
                    self._live_display.bind_admitted(display_attempt, client, admitted_job_id)
                task._display_bind = bind_display
                task._display_callback = lambda packet: self._live_display.offer(display_attempt, packet)
                return task

            previous_job_id = self._queue_job_id
            previous_frozen_request = self._frozen_request
            self._queue_job_id = job_id
            self._frozen_request = base_request
            if self._project_compute is not None:
                self._project_compute.register_job(
                    job_id, module_key="film_optimization", service_owned=False,
                    state="submitting", metadata=metadata)
            next_operation_id = self._operation_serial + 1
            self._attempt_full_request = request
            self._attempt_source_binding = deepcopy(source_binding)
            self._attempt_spec_fingerprint = spec_fingerprint
            self._attempt_editable_snapshot = deepcopy(editable_snapshot)
            self._capture_cancel_attempt(
                next_operation_id, job_id, metadata.get("origin", {}), cancel_event,
                search_mode=search_mode, source_revision=admission_revision)
            self._run_coordinator.start(
                submit=None,
                worker_factory=create_worker,
                edit_epoch=self._source_edit_epoch(),
            )
        except Exception as exc:
            if "display_attempt" in locals():
                self._live_display.end(display_attempt)
            if self._attempt_operation_id == self._operation_serial + 1:
                self._clear_cancel_attempt()
            if self._project_compute is not None and "job_id" in locals():
                self._project_compute.update_job(job_id, "failed")
            if self._project_operation is attempt_project_operation:
                self._project_operation = None
            self._prepared_angle_job_id = None
            self._queue_job_id = locals().get("previous_job_id", self._queue_job_id)
            self._frozen_request = locals().get("previous_frozen_request", self._frozen_request)
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
        if warm_restart is not None and self.session is not None:
            current_source = self.session.snapshot()
            self._source_revision = int(current_source.design_revision)
            self._design = deepcopy(dict(current_source.design))
            self._path = str(current_source.path or "")
            self._source_stale = False
        self._prior_run_fallback = warm_restart[1] if warm_restart is not None else None
        if warm_restart is None:
            self._continuation_record = None
            self._continuation_unavailable_reason = ("独立结构优化暂不支持从取消候选继续。"
                if search_mode == "structure" else "尚无已取消的完整评分候选。")
        self._frozen_request = base_request
        self._synthesis_actual_structure_search_seed = (
            request.structure_search_seed if search_mode == "synthesis" else None)
        self._synthesis_editable_snapshot = deepcopy(editable_snapshot)
        self._completion_handoff_done = False
        self._result = None
        self._synthesis_payload = None
        self._structure_result = None
        self._reduction_result = None
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
        if self._initial_loading:
            self._pending_shortcut_start = True
            return True
        if self._requested_search_mode and not self.requestSearchMode(self._requested_search_mode):
            return False
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

    def _capture_cancel_attempt(self, operation_id: int, job_id: str,
                                origin: Any, cancel_event: Any, *, search_mode=None, source_revision=None) -> None:
        self._attempt_operation_id = int(operation_id)
        self._attempt_job_id = str(job_id)
        self._attempt_session = self.session
        self._attempt_source_controller = self._source_controller
        self._attempt_source_path = (str(self.session.snapshot().path or "")
            if self.session is not None else self._path)
        self._attempt_source_revision = (self._source_revision if source_revision is None
                                         else source_revision)
        self._attempt_edit_epoch = self._source_edit_epoch()
        self._attempt_project_operation = self._project_operation
        self._attempt_origin = deepcopy(dict(origin)) if isinstance(origin, dict) else {}
        self._attempt_cancel_event = cancel_event
        self._attempt_search_mode = search_mode or self._search_mode
        self._attempt_materials_owner = getattr(self.service, "materials", None)
        frozen_request = self._frozen_request
        self._attempt_frozen_material_snapshot = (
            deepcopy(dict(frozen_request.frozen_material_snapshot))
            if frozen_request is not None and frozen_request.frozen_material_snapshot is not None
            else None)
        self._terminal_operation_id = None
        self._attempt_cancel_snapshot = None
        self._cancel_intent_operation_id = None
        self._cancel_intent_job_id = ""
        self._cancel_intent_requested = False
        self._cancel_intent_accepted = False
        self._cancel_intent_consumed = False

    def _clear_cancel_attempt(self) -> None:
        self._attempt_operation_id = None
        self._attempt_job_id = ""
        self._attempt_session = None
        self._attempt_source_controller = None
        self._attempt_source_revision = None
        self._attempt_source_path = None
        self._attempt_edit_epoch = None
        self._attempt_project_operation = None
        self._attempt_origin = {}
        self._attempt_cancel_event = None
        self._attempt_search_mode = ""
        self._attempt_materials_owner = None
        self._attempt_frozen_material_snapshot = None
        self._attempt_cancel_snapshot = None
        self._terminal_operation_id = None
        self._cancel_intent_operation_id = None
        self._cancel_intent_job_id = ""
        self._cancel_intent_requested = False
        self._cancel_intent_accepted = False
        self._cancel_intent_consumed = False

    def _matching_cancel_attempt(self, job_id: str, origin: Any) -> bool:
        if (self._shutting_down or self._attempt_operation_id is None
                or self._attempt_job_id != str(job_id)
                or self._attempt_session is not self.session
                or self._attempt_source_controller is not self._source_controller
                or self._source_closed or self._observers_detached or self._pending_close
                or self._cancel_intent_consumed):
            return False
        if origin is not None and origin != self._attempt_origin:
            return False
        if self._attempt_project_operation is not None:
            if (self._project_compute is None
                    or self._project_operation is not self._attempt_project_operation
                    or not self._project_compute.accepts(self._attempt_project_operation)):
                return False
        return (self._active_operation_id == self._attempt_operation_id
                or self._terminal_operation_id == self._attempt_operation_id
                or (not self._busy
                    and self._operation_serial == self._attempt_operation_id
                    and not self._result_terminal_state))

    def _on_cancel_transport_status(self, job_id: str, code: str) -> None:
        if not self._matching_cancel_attempt(job_id, self._attempt_origin):
            return
        if code:
            self._error = f"Unable to request queue cancellation: {code}"
            self._progress_text = "取消请求尚未确认；可再次点击取消，继续按原作业 ID 重试。"
        else:
            if (self._cancel_intent_requested
                    and bool(getattr(self._attempt_cancel_event, "cancel_acknowledged", False))):
                self._cancel_intent_accepted = True
                self._accept_cancel_intent()
            if self._error.startswith("Unable to request queue cancellation:"):
                self._error = ""
        self.changed.emit()

    def _accept_cancel_intent(self) -> bool:
        if self._attempt_search_mode == "reduction":
            return False
        if (self._cancel_intent_consumed or not self._cancel_intent_accepted
                or not self._cancel_intent_requested
                or self._cancel_intent_operation_id != self._attempt_operation_id
                or self._cancel_intent_job_id != self._attempt_job_id
                or self._terminal_operation_id != self._attempt_operation_id):
            return False
        return self._apply_cancel_candidate_once()

    def _cancel_candidate_verified(self) -> bool:
        if self._attempt_search_mode == "reduction":
            return False
        result = self._result
        if (result is None or result.state != "cancelled" or result.evaluations < 1
                or not result.best_design or not result.per_condition_errors):
            return False
        try:
            if not math.isfinite(float(result.max_normalized_error)):
                return False
        except (TypeError, ValueError, OverflowError):
            return False
        if self._attempt_search_mode == "synthesis":
            payload = self._synthesis_payload
            raw = payload.get("result") if isinstance(payload, dict) else None
            review = payload.get("review") if isinstance(payload, dict) else None
            provenance = raw.get("candidate_provenance") if isinstance(raw, dict) else None
            marker = review.get("cancel_snapshot") if isinstance(review, dict) else None
            if (not isinstance(raw, dict) or not isinstance(provenance, dict)
                    or not isinstance(marker, dict)
                    or provenance.get("selection") != "completed_training_at_cancel"
                    or provenance.get("training_complete") is not True
                    or provenance.get("frozen_at_cancel") is not True
                    or type(provenance.get("fully_scored_evaluations")) is not int
                    or provenance["fully_scored_evaluations"] < 1
                    or marker.get("source") != "worker_completed_reply"
                    or marker.get("replay_performed") is not False
                    or marker.get("candidate_available") is not True
                    or not isinstance(raw.get("training_score"), dict)):
                return False
        elif self._structure_result is not None:
            structure = self._structure_result
            request = getattr(structure, "frozen_request", None)
            marker = (structure.topology_audit.get("cancel_snapshot")
                      if isinstance(structure.topology_audit, Mapping) else None)
            if (request is None or not isinstance(marker, Mapping)
                    or marker.get("source") != "worker_completed_reply"
                    or marker.get("replay_performed") is not False
                    or marker.get("candidate_available") is not True
                    or marker.get("training_complete") is not True):
                return False
            try:
                from coat_sim.core.film_optimization_structure import (
                    validate_film_structure_candidate_topology,
                )
                validate_film_structure_candidate_topology(
                    request, structure.best_design,
                    split_groups=structure.topology_audit.get("split_groups", {}))
            except (ValueError, TypeError, KeyError):
                return False
        else:
            marker = self._attempt_cancel_snapshot
            if not isinstance(marker, dict) or marker.get("candidate_available") is not True:
                return False
        if (self._attempt_source_revision is None
                or result.design_revision != self._attempt_source_revision):
            return False
        try:
            normalized = cast(Any, self.service).validate(deepcopy(dict(result.best_design)))
            if not isinstance(normalized, Mapping):
                return False
        except (ValueError, TypeError, KeyError, AttributeError):
            return False
        if self._attempt_search_mode == "fixed_topology" and not self._matches_frozen_fixed_topology(normalized):
            return False
        return True

    def _matches_frozen_fixed_topology(self, candidate: Mapping[str, Any]) -> bool:
        request = self._frozen_request
        if request is None:
            return False
        frozen = request.design_snapshot
        for key in set(frozen) | set(candidate):
            if key in {"front_layers", "back_layers"}:
                continue
            expected = frozen.get(key)
            actual = candidate.get(key)
            if expected != actual:
                # The thickness solver deliberately detaches a nonempty,
                # synchronized expression after writing physical thicknesses.
                # Permit only that deterministic flag transition; text,
                # symbols and every other expression field stay frozen.
                if (key != "expression" or not isinstance(expected, Mapping)
                        or not expected.get("text") or expected.get("in_sync") is not True):
                    return False
                detached = deepcopy(dict(expected))
                detached["in_sync"] = False
                if detached != actual:
                    return False
        for side in ("front_layers", "back_layers"):
            base_rows = list(frozen.get(side, ()))
            candidate_rows = list(candidate.get(side, ()))
            if len(base_rows) != len(candidate_rows):
                return False
            for base, row in zip(base_rows, candidate_rows, strict=True):
                if base.get("id") != row.get("id"):
                    return False
                base_metadata = {key: value for key, value in base.items() if key != "thickness"}
                candidate_metadata = {key: value for key, value in row.items() if key != "thickness"}
                if base_metadata != candidate_metadata:
                    return False
        return True

    def _cancel_material_references_unchanged(self, design: Mapping[str, Any]) -> bool:
        materials = getattr(self.service, "materials", None)
        if materials is None or materials is not self._attempt_materials_owner:
            return False
        snapshot = self._attempt_frozen_material_snapshot
        if snapshot is None:
            return self._attempt_search_mode != "synthesis"
        try:
            from coat_sim.core.film_design_service import active_material_references
            frozen_rows = {row["id"]: row for row in snapshot.get("materials", [])}
            for ref in active_material_references(design):
                material_id = ref["material_id"]
                set_id = ref["optical_set_id"]
                expected = next((row for row in frozen_rows[material_id].get("optical_sets", [])
                                 if row.get("id") == set_id), None)
                material = materials.get(material_id)
                if material is None:
                    return False
                actual = next((row for row in material.get("optical_sets", [])
                               if row.get("id") == set_id), None)
                if expected is None or actual != expected:
                    return False
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
        return True

    def _apply_cancel_candidate_once(self) -> bool:
        if self._attempt_search_mode == "reduction":
            return False
        if self._cancel_intent_consumed:
            return False
        self._cancel_intent_consumed = True
        if not self._cancel_candidate_verified():
            self._progress_text = "取消时没有可用的完整有效候选，原设计保持不变。"
            self.changed.emit()
            return False
        result = self._result
        if result is None:
            return False
        if not self._cancel_material_references_unchanged(result.best_design):
            self._source_stale = True
            self._error = "Materials referenced by the candidate changed during the run; the candidate is retained and was not applied."
            self.changed.emit()
            return False
        if (self._attempt_session is not self.session
                or self._attempt_source_controller is not self._source_controller
                or self.session is None or self._source_controller is None
                or self._source_closed or self._shutting_down):
            self._source_stale = True
            self._error = "计算期间源设计或项目版本已变化；候选已保留供查看，未自动应用。"
            self.changed.emit()
            return False
        if self._project_compute is not None and (
                self._attempt_project_operation is None
                or not self._project_compute.accepts(self._attempt_project_operation)):
            self._source_stale = True
            self._error = "计算期间源设计或项目版本已变化；候选已保留供查看，未自动应用。"
            self.changed.emit()
            return False
        snapshot = self.session.snapshot()
        if (snapshot.design_revision != self._attempt_source_revision
                or self._source_edit_epoch() != self._attempt_edit_epoch):
            self._source_stale = True
            self._error = "计算期间源设计或项目版本已变化；候选已保留供查看，未自动应用。"
            self.changed.emit()
            return False
        try:
            current = cast(Any, self.service).validate(deepcopy(dict(snapshot.design)))
            candidate = cast(Any, self.service).validate(deepcopy(dict(self._result.best_design)))
            same_design = current == candidate
        except (ValueError, TypeError, KeyError, AttributeError):
            same_design = False
        if same_design:
            self._candidate_applied = True
            self._load_source(preserve_variables=True)
        else:
            self._operation_edit_epoch = self._attempt_edit_epoch
            if not self._apply_candidate():
                self._source_stale = True
                self._error = self._error or "候选未通过源版本校验；原设计保持不变。"
                self.changed.emit()
                return False
        self._capture_continuation_postapply()
        self._progress_text = "取消后已将当前最佳候选应用到原设计并请求刷新光谱；未保存文件。"
        opener = getattr(self._source_controller, "openSimulationAndCalculate", None)
        try:
            if callable(opener) and not opener():
                raise ValueError("same-project spectrum route did not confirm refresh")
        except (ValueError, RuntimeError, TypeError) as exc:
            self._error = f"候选已应用，但光谱刷新未完成：{exc}"
        self.changed.emit()
        self.plotDataChanged.emit()
        return True

    def _numeric_cleanup_notice(self) -> str:
        payload = self._synthesis_payload
        if payload is None:
            return ""
        search = payload["result"].get("performance", {}).get("search", {})
        fallback = search.get("numeric_zero_fallback")
        if not isinstance(fallback, Mapping) or not fallback.get("unpublished_better_trials"):
            return ""
        disposition = ("已应用此前完整候选。" if self._candidate_applied else
                       "已保留此前完整候选；原设计保持不变。" if fallback.get("returned_training_complete") else
                       "没有完整候选；原设计保持不变。")
        reason = ("未通过光谱等价检查" if fallback.get("spectral_equivalence_rejections") else "未完成清理复算")
        return "含数值零层的潜在更优试探" + reason + "；" + disposition

    @Slot(str, object, object, result=bool)
    def onProjectQueueCancelAccepted(self, job_id: str, cancellation_result: Any,
                                    origin: Any) -> bool:
        accepted = (cancellation_result is True or
                    isinstance(cancellation_result, dict) and bool(
                        cancellation_result.get("cancel_requested")
                        or cancellation_result.get("state") == "cancelled"))
        if not accepted or not self._matching_cancel_attempt(job_id, origin):
            return False
        self._cancel_intent_operation_id = self._attempt_operation_id
        self._cancel_intent_job_id = self._attempt_job_id
        self._cancel_intent_requested = True
        self._cancel_intent_accepted = True
        self._accept_cancel_intent()
        return True

    @Slot()
    def cancelOptimization(self) -> None:
        if self._manufacturing_controller is not None and self._manufacturing_controller.busy:
            self._manufacturing_controller.cancel()
            return
        self._request_optimization_cancel(user_initiated=True)

    def _request_optimization_cancel(self, *, user_initiated: bool) -> None:
        if self._busy and self._cancel_event is not None:
            if user_initiated:
                self._cancel_intent_operation_id = self._attempt_operation_id
                self._cancel_intent_job_id = self._attempt_job_id
                self._cancel_intent_requested = self._attempt_operation_id is not None
            error = self._run_coordinator.request_cancel()
            if error is not None:
                detail = str(getattr(self._cancel_event, "cancel_error_message", "")
                             or getattr(self._cancel_event, "cancel_error_code", "")
                             or f"{type(error).__name__}: {error}")
                self._error = f"Unable to request queue cancellation: {detail}"
            elif self._error.startswith("Unable to request queue cancellation:"):
                self._error = ""
            self._progress_text = "正在取消；等待当前完整评估结束"
            self.changed.emit()

    def inspectQueueResult(self, job_id: str, payload: dict, *, origin=None, source_context=None) -> bool:
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
            from coat_sim.core.film_optimization_structure import FilmStructureOptimizationResult
            from coat_sim.core.film_reduction import FilmReductionResult

            candidate = _film_result_from_queue(payload)
            if isinstance(candidate, (FilmStructureOptimizationResult, FilmReductionResult)):
                frozen_request = candidate.frozen_request
                if frozen_request is None:
                    raise ValueError("queue result has no frozen structure request")
                request = frozen_request.base_request
                result = candidate.base_result
            else:
                fixed_request = deserialize_request(payload["request"])
                if not isinstance(fixed_request, FilmOptimizationRequest):
                    raise ValueError("fixed-topology queue result requires a matching optimization request")
                request = fixed_request
                result = candidate
            objectives = []
            for index, objective in enumerate(request.objectives):
                row = _objective_default(index)
                row.update(wavelength_min_nm=objective.wavelength_min_nm,
                           wavelength_max_nm=objective.wavelength_max_nm,
                           wavelength_samples=objective.wavelength_samples,
                           sampling_basis="samples",
                           wavelength_step_nm=(0.0 if objective.wavelength_samples == 1 else
                               (objective.wavelength_max_nm - objective.wavelength_min_nm) /
                               (objective.wavelength_samples - 1)),
                           actual_wavelength_step_nm=(0.0 if objective.wavelength_samples == 1 else
                               (objective.wavelength_max_nm - objective.wavelength_min_nm) /
                               (objective.wavelength_samples - 1)),
                           angle_sampling_mode="points",
                           angles_text=", ".join(str(value) for value in objective.angles_deg),
                           polarization=objective.polarization, metric=objective.metric,
                           pointwise=_constraint_editor_from_request(objective.pointwise),
                           mean=_constraint_editor_from_request(objective.mean))
                row = _resolve_angle_sampling(row)
                objectives.append(row)
        except Exception as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self._live_display.clear_analysis()
        self._project_operation = operation
        self._queue_job_id = str(job_id)
        self._frozen_request = request
        self._result = result
        self._reduction_result = candidate if isinstance(candidate, FilmReductionResult) else None
        self._structure_result = candidate if isinstance(candidate, FilmStructureOptimizationResult) else None
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
        self._synthesis_payload = None
        if self._reduction_result is None:
            self._bind_history_continuation(job_id, request, result, payload, source_context,
                "structure" if self._structure_result is not None else "fixed_topology")
        else:
            self._continuation_record = None
            self._continuation_unavailable_reason = "减层 / 减厚历史候选仅供查看或另存。"
        self._error = ""
        self.changed.emit()
        self.plotDataChanged.emit()
        return True

    def inspectSynthesisQueueResult(self, job_id: str, payload: dict, *, origin=None, source_context=None) -> bool:
        """Inspect a factory-verified same-project typed synthesis result, read only."""
        try:
            if self.project_context is None or self._project_compute is None:
                raise ValueError("Synthesis history requires an explicit project")
            if (not isinstance(origin, dict)
                    or origin.get("project_id") != self.project_context.binding.project_id
                    or origin.get("project_root") != str(self.project_context.binding.root)):
                raise ValueError("Synthesis result belongs to a different project")
            if self._busy or self._pending_edit_error or self._shutting_down:
                raise ValueError("Finish the active operation or edit before inspecting history")
            if self._result is not None and not self._candidate_saved and not self._candidate_applied:
                raise ValueError("Save or discard the current candidate before inspecting history")
            operation = self._project_compute.capture("inspect-" + str(job_id))
            if not self._project_compute.accepts(operation):
                raise ValueError("The originating project generation is no longer current")
            view = synthesis_result_view(payload)
            from coat_sim.core.film_synthesis_io import SCHEMA_VERSION, film_synthesis_request_from_payload
            envelope = {"schema_version": SCHEMA_VERSION, "kind": "coat_sim.film_synthesis_request",
                        "request_id": "history-" + str(job_id)[:100], "physical_structures": [],
                        "request": payload["result"]["frozen_request"]}
            _, request, _ = film_synthesis_request_from_payload(envelope)
            objectives = []
            for index, objective in enumerate(request.objectives):
                row = _objective_default(index)
                row.update(wavelength_min_nm=objective.wavelength_min_nm,
                           wavelength_max_nm=objective.wavelength_max_nm,
                           wavelength_samples=objective.wavelength_samples,
                           metric=objective.metric, polarization=objective.polarization,
                           pointwise=_constraint_editor_from_request(objective.pointwise),
                           mean=_constraint_editor_from_request(objective.mean),
                           angle_sampling_mode="points", angles_text=", ".join(str(v) for v in objective.angles_deg))
                objectives.append(row)
        except (ValueError, TypeError, KeyError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self._live_display.clear_analysis()
        self._project_operation = operation
        self._queue_job_id = str(job_id)
        self._synthesis_payload = deepcopy(dict(view.payload))
        self._synthesis_editable_snapshot = None
        self._result = view.optimization_result
        self._frozen_request = synthesis_base_request(request)
        self._synthesis_actual_structure_search_seed = request.structure_search_seed
        self._structure_result = None
        self._reduction_result = None
        self._inspection_objectives = objectives
        self._result_application_eligible = False
        self._operation_edit_epoch = None
        self._baseline = {}
        self._candidate_saved = self._candidate_applied = False
        self._source_stale = True
        self._plot_objective = 0
        self._plot_angle = float(request.objectives[0].angles_deg[0])
        self._result_terminal_state = "历史合成结果 / " + str(payload["result"]["state"])
        self._progress_text = "只读历史合成候选；可预览/另存，不能应用到当前设计"
        self._bind_history_continuation(job_id, request, self._result, payload,
            source_context, "synthesis")
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
        cancel_error = str(getattr(self._cancel_event, "cancel_error_message", "")
                           or getattr(self._cancel_event, "cancel_error_code", "") or "")
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
        target_changed = (pending.get("target_identity")
                          != self._compute_target.selection_identity())
        if source_changed or project_changed or target_changed:
            self._project_operation = None
            self._prepared_angle_job_id = None
            self._source_stale = True
            self._error = ("项目已关闭或正在关闭；未提交队列作业。"
                           if project_changed else
                           "计算设备在准备角度网格期间发生改变；未提交队列作业。"
                           if target_changed else
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
        result = replace(result, display_score_config=pending.get("display_score_config"))
        if not self._start_optimization(prepared_start=result):
            self._project_operation = None
            self._prepared_angle_job_id = None

    @Slot(int, object, object, object)
    def _on_finished(self, operation_id: int, result, baseline_rows, error) -> None:
        pending = self._pending_angle_preparation
        if pending is not None and pending.get("operation_id") == operation_id:
            self._on_angle_preparation_finished(operation_id, result, error)
            return
        if (self._attempt_operation_id == operation_id
                and operation_id == self._operation_serial):
            self._terminal_operation_id = operation_id
        if isinstance(result, _FilmOptimizationTerminal):
            self._attempt_cancel_snapshot = deepcopy(result.cancel_snapshot)
            result = result.result
            if (getattr(result, "state", None) == "cancelled"
                    and isinstance(result.cancel_snapshot if hasattr(result, "cancel_snapshot") else
                                   self._attempt_cancel_snapshot, dict)):
                error = None
        project_current = self._project_current()
        if (self._project_compute is not None and operation_id == self._operation_serial
                and project_current):
            state = ("submission_unknown" if isinstance(error, dict) and error.get("state") == "submission_unknown"
                     else "cancelled" if isinstance(error, dict) and error.get("state") == "cancelled"
                     else "failed" if error else "succeeded")
            self._project_compute.update_job(self._queue_job_id, state)
            if self._project_compute.editing_frozen and not self._shutting_down:
                self._pending_project_completion = (operation_id, result, baseline_rows, error)
                return
        if self._shutting_down or operation_id != self._operation_serial:
            return
        self._live_display.end(("optimization", operation_id))
        synthesis_result = result if isinstance(result, SynthesisQuickResult) else None
        self._synthesis_payload = deepcopy(dict(synthesis_result.payload)) if synthesis_result is not None else None
        if synthesis_result is not None:
            result = synthesis_result.optimization_result
            synthesis_payload = self._synthesis_payload
            raw_cancel = synthesis_payload.get("result", {}) if synthesis_payload is not None else {}
            review = synthesis_payload.get("review", {}) if synthesis_payload is not None else {}
            cancel_marker = review.get("cancel_snapshot") if isinstance(review, dict) else None
            if (isinstance(raw_cancel, dict) and raw_cancel.get("state") == "cancelled"
                    and isinstance(cancel_marker, dict)
                    and cancel_marker.get("source") == "worker_completed_reply"
                    and cancel_marker.get("replay_performed") is False):
                error = None
            if result is None:
                self._result = None
                raw = synthesis_result.payload["result"]
                self._result_terminal_state = str(raw["state"])
                self._progress_text = str(raw["message"])
                self._error = str(error or "") if raw["state"] == "failed" else ""
                self._result_application_eligible = False
                self.changed.emit()
                self.plotDataChanged.emit()
                if self._cancel_intent_accepted:
                    self._accept_cancel_intent()
                if self._run_coordinator.take_pending_close():
                    self.requestClose()
                return
        from coat_sim.core.film_reduction import FilmReductionResult
        self._reduction_result = result if isinstance(result, FilmReductionResult) else None
        structure_result = (result if self._reduction_result is None and hasattr(result, "base_result")
                            and hasattr(result, "topology_audit") else None)
        if structure_result is not None:
            result = structure_result.base_result
        self._structure_result = structure_result
        if self._reduction_result is not None:
            result = self._reduction_result.base_result
        if (structure_result is not None
                and isinstance(structure_result.topology_audit, Mapping)):
            marker = structure_result.topology_audit.get("cancel_snapshot")
            if isinstance(marker, Mapping):
                self._attempt_cancel_snapshot = deepcopy(dict(marker))
        has_candidate = isinstance(result, FilmOptimizationResult)
        self._result_application_eligible = bool(
            not error and has_candidate and result.state == "completed"
            and (structure_result is None or (
                getattr(structure_result, "is_final", False)
                and result.validation_complete)))
        if self._reduction_result is not None:
            self._result_application_eligible = self._reduction_eligible()
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
        if not project_current:
            self._source_stale = True
            self._error = "Source project generation changed; the candidate is retained read-only."
        if has_candidate and result.state == "cancelled" and self._cancel_candidate_verified():
            self._result_application_eligible = True
            if not self._cancel_material_references_unchanged(result.best_design):
                self._source_stale = True
                self._error = "Materials referenced by the candidate changed during the run; the candidate is retained for review."
        if isinstance(error, dict) and error.get("state") == "submission_unknown":
            job_id = str(error.get("job_id") or self._queue_job_id)
            self._result_terminal_state = "提交状态未知"
            self._error = (
                "队列提交状态仍未知；不会自动重复提交。"
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
        if self._synthesis_payload is not None and not error:
            task_state = self._synthesis_payload["result"]["state"]
            if task_state == "not_found":
                self._result_terminal_state = "未找到达标候选"
        if has_candidate and result.state == "cancelled":
            self._result_terminal_state = "取消"
            if not isinstance(error, dict) or error.get("state") == "cancelled":
                self._error = ""
            self._progress_text = "已结束取消；保留同一计算中完整评分的候选供查看，未写入文件。"
        if (has_candidate and result.state == "cancelled"
                and self._cancel_candidate_verified()
                and not self._cancel_material_references_unchanged(result.best_design)):
            self._source_stale = True
            self._error = "Materials referenced by the candidate changed during the run; the candidate is retained for review."
        if not project_current:
            self._error = "Source project generation changed; the candidate is retained read-only."
            self._progress_text = "The source project changed during this run; its candidate was retained without applying."
        if has_candidate and result.state == "cancelled":
            self._retain_cancelled_restart()
        elif has_candidate and result.evaluations > 0 and result.per_condition_errors:
            self._continuation_record = None
            self._continuation_unavailable_reason = "只有已取消并保留完整评分候选的作业可以继续。"
        if has_candidate and result.evaluations > 0 and result.per_condition_errors:
            self._prior_run_fallback = None
        self.changed.emit()
        self.plotDataChanged.emit()
        if (self._terminal_operation_id == self._attempt_operation_id
                and self._cancel_intent_accepted):
            self._accept_cancel_intent()
        if (self._reduction_result is None and self._attempt_search_mode != "reduction"
                and not error and isinstance(result, FilmOptimizationResult)
                and result.state == "completed"):
            self._apply_completed_result(operation_id, result)
        if self._run_coordinator.take_pending_close():
            self.requestClose()

    def _apply_completed_result(self, operation_id: int,
                                result: FilmOptimizationResult) -> bool:
        if self._attempt_search_mode == "reduction" or self._reduction_result is not None:
            return False
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
        if self._candidate_applied and self._result is result:
            return True
        snapshot = session.snapshot()
        try:
            normalized_candidate = cast(Any, self.service).validate(deepcopy(dict(result.best_design)))
            candidate_design = normalized_candidate
            if self._structure_result is None and self._synthesis_payload is None:
                candidate_design = deepcopy(dict(snapshot.design))
                for side in ("front_layers", "back_layers"):
                    current_ids = [layer.get("id") for layer in snapshot.design.get(side, ())]
                    candidate_ids = [layer.get("id") for layer in normalized_candidate.get(side, ())]
                    if (candidate_ids != current_ids or len(set(candidate_ids)) != len(candidate_ids)):
                        self._error = "优化结果膜层 ID 与来源不一致；未自动覆盖设计。"
                        self.changed.emit()
                        return False
                    for current, candidate in zip(candidate_design.get(side, ()),
                                                  normalized_candidate.get(side, ()), strict=True):
                        current["thickness"] = deepcopy(candidate["thickness"])
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self._error = "优化最终候选无效；未覆盖设计：" + str(exc)
            self.changed.emit()
            return False
        if (snapshot.design_revision != result.design_revision
                or self._source_edit_epoch() != self._operation_edit_epoch):
            self._source_stale = True
            self._error = "原设计在优化期间发生编辑（包括尚未提交的字段）；候选未自动覆盖。"
            self.changed.emit()
            return False
        apply = getattr(source, "applySynthesizedDesign" if self._synthesis_payload is not None
                        else "applyOptimizedDesign", None)
        if not callable(apply):
            self._error = "来源设计窗口不支持安全回写；优化候选已保留。"
            self.changed.emit()
            return False
        try:
            args = (candidate_design, int(result.design_revision),
                    int(self._operation_edit_epoch or 0))
            if self._synthesis_payload is not None:
                if (self._frozen_request is None
                        or self._frozen_request.frozen_material_snapshot is None
                        or self._synthesis_editable_snapshot is None):
                    raise ValueError("Synthesis candidate has no submission-time editable material closure")
                accepted = bool(apply(*args, deepcopy(self._synthesis_editable_snapshot)))
            else:
                accepted = bool(apply(*args))
        except Exception as exc:
            self._error = f"自动回写失败：{type(exc).__name__}: {exc}"
            self.changed.emit()
            return False
        if not accepted:
            self._source_stale = True
            self._error = "原设计或未提交编辑已改变；候选未自动覆盖。"
            self.changed.emit()
            return False
        self._candidate_applied = True
        self._candidate_saved = True
        self._error = ""
        self._load_source(preserve_variables=True)
        if result.targets_met:
            self._progress_text = str(result.message or "优化完成") + " · 已自动回写原设计。"
        else:
            self._progress_text = (str(result.message or "优化完成")
                                   + " · 采样目标未全部满足；候选已回写，请核对结果。")
        self.changed.emit()
        self.plotDataChanged.emit()
        # Existing observers refresh an already-open spectrum; this route also
        # creates/restores the same-project singleton when no spectrum exists.
        opener = getattr(source, "openSimulationAndCalculate", None)
        if callable(opener):
            try:
                if not opener():
                    raise ValueError("光谱仿真窗口未确认打开")
            except (ValueError, RuntimeError, TypeError) as exc:
                self._error = "优化厚度已应用，但光谱刷新未完成：" + str(exc)
                self.changed.emit()
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
        if self._synthesis_payload is not None:
            self.applyConfirmationRequested.emit("确认用已审计合成候选替换当前完整层序？原设计仅在确认后改变。")
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
        apply = getattr(self._source_controller,
                        "applyReducedDesign" if self._reduction_result is not None else
                        "applySynthesizedDesign" if self._synthesis_payload is not None else "applyOptimizedDesign", None)
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
            args = (deepcopy(dict(result.best_design)), int(result.design_revision), int(edit_epoch))
            if self._reduction_result is not None:
                if not self._reduction_eligible():
                    raise ValueError("Reduction candidate has not passed complete independent audit")
                if (self._attempt_full_request != self._reduction_result.frozen_request
                        or self._attempt_session is not self.session
                        or self._attempt_source_controller is not self._source_controller):
                    raise ValueError("Reduction candidate source/request identity differs")
                if not self._cancel_material_references_unchanged(
                        self._reduction_result.frozen_request.base_request.design_snapshot):
                    raise ValueError("Reduction material references changed; candidate retained read-only")
                # The queue reader already verified complete raw coverage and
                # optics off-thread. Recheck topology here without replaying it.
                from coat_sim.core.film_reduction import validate_film_reduction_candidate_topology
                validate_film_reduction_candidate_topology(
                    self._reduction_result.frozen_request, result.best_design)
                accepted = bool(apply(*args, self._reduction_result.frozen_request))
            elif self._synthesis_payload is not None:
                if self._frozen_request is None or self._frozen_request.frozen_material_snapshot is None:
                    raise ValueError("Synthesis candidate has no frozen optical closure")
                closure = self._synthesis_editable_snapshot
                if closure is None:
                    raise ValueError("Synthesis candidate has no submission-time editable material closure")
                accepted = bool(apply(*args, deepcopy(closure)))
            else:
                accepted = bool(apply(*args))
        except Exception as exc:
            self._error = f"应用候选失败：{type(exc).__name__}: {exc}"
            self.changed.emit()
            return False
        if not accepted and self._synthesis_payload is not None:
            snapshot = session.snapshot()
            detail = str(getattr(snapshot, "error", "") or "")
            self._error = ("合成候选未应用：" + detail if detail else
                           "原设计版本已变化或合成候选校验失败；候选未应用。")
            self._source_stale = (snapshot.design_revision != result.design_revision
                                  or self._source_edit_epoch() != edit_epoch)
            self.changed.emit()
            return False
        if not accepted:
            self._error = "原设计版本已变化或候选无效；整个结果均未应用。"
            self._source_stale = True
            self.changed.emit()
            return False
        self._candidate_applied = True
        self._candidate_saved = True
        self._error = ""
        self._load_source(preserve_variables=True)
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
            if self._synthesis_payload is not None and self._synthesis_editable_snapshot is not None:
                frozen = self._synthesis_editable_snapshot
            rows = frozen["materials"] if frozen is not None else project_service.materials._records()
            materials = {row["id"]: row for row in rows}
            if self._synthesis_payload is not None:
                from coat_sim.core.project_dependencies import document_material_references
                missing = [f"{material_id}/{set_id}" for material_id, set_id in
                    document_material_references("film", design)
                    if material_id not in materials or set_id not in {
                        item["id"] for item in materials[material_id]["optical_sets"]}]
                if missing:
                    raise ValueError("合成历史缺少完整可编辑材料出处：" + ", ".join(missing)
                        + "；已有 active 光学快照仍可查看/冻结预览，无法另存完整项目处方")
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
            if self._synthesis_payload is not None:
                if self._frozen_request is None:
                    raise ValueError("Synthesis candidate has no frozen request")
                verify_synthesis_file_references(self.service, self._frozen_request, self._result.best_design)
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
        if self._manufacturing_controller is not None and self._manufacturing_controller.busy:
            self._manufacturing_controller.requestClose()
        if not self.busy and not self.finishSynthesisProgressPolicyEdit():
            return
        if (not self._busy and self._search_mode == "synthesis"
                and self._synthesis_structure_search_seed_error):
            self.finishSynthesisStructureSearchSeedEdit()
            return
        if not self._busy and not self._commit_pending_angle_draft():
            self.changed.emit()
            return
        if self._initial_loading:
            self._initial_load_closed = True
            self._pending_shortcut_start = False
            self.detach_observers()
            self.closeConfirmed.emit()
            return
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
        self._live_display.shutdown()
        sensitivity_done = (self._sensitivity_controller is None
            or self._sensitivity_controller.shutdown(timeout_ms))
        manufacturing_done = (self._manufacturing_controller is None
            or self._manufacturing_controller.shutdown(timeout_ms))
        self._synthesis_forward_probe.shutdown()
        self._pending_project_completion = None
        if self._project_compute is not None:
            self._project_compute.unregister_thaw_callback(self._on_project_unfrozen)
        return self._run_coordinator.shutdown(timeout_ms) and sensitivity_done and manufacturing_done

