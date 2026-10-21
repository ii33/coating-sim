"""Run-owned sparse audit points and immutable local-refinement guides.

Guides are private optimizer values, never training scores or audit evidence.
The caller owns complete-audit admission, physical evaluation, budgets and
cancellation. Each local invocation must keep one snapshot for its lifetime.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import math
from typing import TYPE_CHECKING, Any, Callable, Mapping

from .film_optimization import FilmOptimizationObjective
from .film_optimization_scoring import score_condition

if TYPE_CHECKING:
    from .film_auto_synthesis import FilmSynthesisRequest


@dataclass(frozen=True, order=True)
class FeedbackPoint:
    objective_index: int
    wavelength_nm: float
    angle_deg: float
    polarization: str


@dataclass(frozen=True)
class FeedbackSnapshot:
    revision: int
    points: tuple[FeedbackPoint, ...]
    objectives: tuple[FilmOptimizationObjective, ...]
    acceptable_normalized_error: float
    strict_lower_bound_objective_indices: tuple[int, ...]


def _field(error: Any, name: str) -> Any:
    return error[name] if isinstance(error, Mapping) else getattr(error, name)


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("feedback condition requires finite numeric values")
    return float(value)


def _checked_error(error: Any, objectives: tuple[FilmOptimizationObjective, ...]) -> tuple[FeedbackPoint, float, float]:
    index = _field(error, "objective_index")
    if type(index) is not int or not 0 <= index < len(objectives):
        raise ValueError("feedback objective index is invalid")
    objective = objectives[index]
    wave, angle = _number(_field(error, "wavelength_nm")), _number(_field(error, "angle_deg"))
    if (not objective.wavelength_min_nm <= wave <= objective.wavelength_max_nm
            or angle not in objective.angles_deg
            or any(_field(error, key) != getattr(objective, key)
                   for key in ("polarization", "metric", "target_value", "lower_bound", "upper_bound"))):
        raise ValueError("feedback condition differs from its original objective")
    actual = _number(_field(error, "actual"))
    if objective.mode == "value":
        if objective.target_value is None:
            raise ValueError("feedback value objective requires a target")
        violation = abs(actual - objective.target_value)
    else:
        violation = max(0., 0. if objective.lower_bound is None else objective.lower_bound - actual,
            0. if objective.upper_bound is None else actual - objective.upper_bound)
    normalized, _ = score_condition(violation, objective.normalization_scale, objective.weight)
    if not math.isclose(_number(_field(error, "normalized_violation")), normalized,
                        rel_tol=1e-10, abs_tol=1e-12):
        raise ValueError("feedback violation differs from its original objective")
    return FeedbackPoint(index, wave, angle, objective.polarization), actual, normalized


def _strict_failed(index: int, actual: float, objectives: tuple[FilmOptimizationObjective, ...],
                   strict_indices: tuple[int, ...]) -> bool:
    if index not in strict_indices:
        return False
    lower = objectives[index].lower_bound
    if lower is None:
        raise ValueError("strict feedback objective requires an explicit lower bound")
    return actual <= lower


def _same_wave(left: float, right: float) -> bool:
    return abs(left - right) <= 4 * max(math.ulp(left), math.ulp(right))


def _on_training_grid(point: FeedbackPoint, objective: FilmOptimizationObjective) -> bool:
    low, high, count = objective.wavelength_min_nm, objective.wavelength_max_nm, objective.wavelength_samples
    if count == 1 or low == high:
        return _same_wave(point.wavelength_nm, low)
    index = round((point.wavelength_nm - low) / (high - low) * (count - 1))
    grid_wave = low + (high - low) * index / (count - 1)
    return _same_wave(point.wavelength_nm, grid_wave)


class SparseFeedbackRegistry:
    """Accumulate worst failing conditions from caller-committed complete audits.

    This does not verify that an audit was completed: call only after the
    parent's existing complete-audit commit. Validate every row before mutation.
    One worst failure per objective/angle/polarization is admitted per audit;
    exact condition identities are retained without wavelength rounding.
    """

    def __init__(self, request: FilmSynthesisRequest):
        self._objectives = tuple(request.objectives)
        self._acceptable = request.acceptable_normalized_error
        self._strict = tuple(request.strict_lower_bound_objective_indices)
        self._points: set[FeedbackPoint] = set()
        self._revision = 0

    def record_audit(self, score: Mapping[str, Any]) -> int:
        worst: dict[tuple[int, float, str], tuple[tuple[bool, float, float], FeedbackPoint]] = {}
        for error in score["errors"]:
            point, actual, normalized = _checked_error(error, self._objectives)
            strict_failure = _strict_failed(point.objective_index, actual, self._objectives, self._strict)
            if normalized <= self._acceptable and not strict_failure:
                continue
            if _on_training_grid(point, self._objectives[point.objective_index]):
                continue
            if any(old.objective_index == point.objective_index
                   and old.angle_deg == point.angle_deg and old.polarization == point.polarization
                   and _same_wave(old.wavelength_nm, point.wavelength_nm) for old in self._points):
                continue
            group = (point.objective_index, point.angle_deg, point.polarization)
            severity = (strict_failure, normalized, -point.wavelength_nm)
            if group not in worst or severity > worst[group][0]:
                worst[group] = (severity, point)
        additions = {entry[1] for entry in worst.values()} - self._points
        if additions:
            self._points.update(additions)
            self._revision += 1
        return len(additions)

    def snapshot(self) -> FeedbackSnapshot:
        return FeedbackSnapshot(self._revision, tuple(sorted(self._points)),
            self._objectives, self._acceptable, self._strict)


def augment_feedback_guide(request: FilmSynthesisRequest, base: Mapping[str, Any],
                           snapshot: FeedbackSnapshot, *,
                           evaluate_points: Callable[[tuple[FilmOptimizationObjective, ...]], Mapping[str, Any]]) -> dict[str, Any]:
    """Add singleton responses to a private copy; keep the original base intact.

    ``evaluate_points`` evaluates the same design with these temporary singleton
    objectives and the same requested Jacobian. It must use the parent's physical
    accounting and stop checks. The resulting score is temporary guide metadata;
    original grid errors/RMS/weights remain unmodified. Restore before archiving
    or export. No callback occurs for an empty snapshot.
    """
    if (tuple(request.objectives) != snapshot.objectives
            or request.acceptable_normalized_error != snapshot.acceptable_normalized_error
            or tuple(request.strict_lower_bound_objective_indices) != snapshot.strict_lower_bound_objective_indices):
        raise ValueError("feedback snapshot belongs to different objective conditions")
    if "_feedback_base" in base:
        raise ValueError("a feedback guide cannot be augmented twice")
    for point in snapshot.points:
        if type(point.objective_index) is not int or not 0 <= point.objective_index < len(snapshot.objectives):
            raise ValueError("feedback snapshot objective index is invalid")
        objective = snapshot.objectives[point.objective_index]
        if (not objective.wavelength_min_nm <= _number(point.wavelength_nm) <= objective.wavelength_max_nm
                or _number(point.angle_deg) not in objective.angles_deg
                or point.polarization != objective.polarization):
            raise ValueError("feedback snapshot point differs from its original objective")
    base_errors = [_checked_error(error, snapshot.objectives) for error in base["score"]["errors"]]
    strict_failed = any(_strict_failed(p.objective_index, actual, snapshot.objectives,
        snapshot.strict_lower_bound_objective_indices) for p, actual, _ in base_errors)
    guide = deepcopy(dict(base))
    responses = list(guide.get("responses", ()))
    maximum = _number(base["score"]["max_error"])
    if snapshot.points:
        if not responses:
            raise ValueError("feedback refinement requires complete base responses")
        objectives = tuple(replace(snapshot.objectives[p.objective_index],
            wavelength_min_nm=p.wavelength_nm, wavelength_max_nm=p.wavelength_nm,
            wavelength_samples=1, angles_deg=(p.angle_deg,)) for p in snapshot.points)
        extra = evaluate_points(objectives)
        if extra["design"] != base["design"]:
            raise ValueError("feedback response belongs to another design")
        errors, extra_responses = tuple(extra["score"]["errors"]), tuple(extra["responses"])
        if len(errors) != len(objectives) or len(extra_responses) != len(objectives):
            raise ValueError("feedback evaluation is missing singleton conditions")
        by_index = {}
        for error in errors:
            local, actual, normalized = _checked_error(error, objectives)
            if local.objective_index in by_index:
                raise ValueError("feedback evaluation repeats a singleton condition")
            by_index[local.objective_index] = actual
            point = snapshot.points[local.objective_index]
            maximum = max(maximum, normalized)
            strict_failed = strict_failed or _strict_failed(point.objective_index, actual,
                snapshot.objectives, snapshot.strict_lower_bound_objective_indices)
        seen = set()
        for response in extra_responses:
            local_index = response["objective_index"]
            if type(local_index) is not int or local_index not in by_index or local_index in seen:
                raise ValueError("feedback response objective index is invalid")
            seen.add(local_index)
            point = snapshot.points[local_index]
            if (response["angle_deg"] != point.angle_deg
                    or tuple(response["wavelength_nm"]) != (point.wavelength_nm,)
                    or tuple(response["actual_values"]) != (by_index[local_index],)):
                raise ValueError("feedback response differs from its singleton condition")
            remapped = deepcopy(dict(response))
            remapped["objective_index"] = point.objective_index
            responses.append(remapped)
    guide["responses"] = tuple(responses)
    guide["score"]["max_error"] = maximum
    guide["_feedback_base"] = base
    guide["_feedback_revision"] = snapshot.revision
    guide["_feedback_strict_failed"] = strict_failed
    return guide


def restore_feedback_base(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the exact base value; no guide fields or responses cross export."""
    return value.get("_feedback_base", value)


def feedback_candidate_key(value: Mapping[str, Any], loss: float) -> tuple[int, float, float]:
    """Rank only candidates from one fixed snapshot inside a local invocation.

    Strict equality remains a failure even when raw normalized violation is zero.
    Interior margins enter ``loss`` only; independent acceptance is unchanged.
    """
    return (int(bool(value.get("_feedback_strict_failed", False))),
            _number(value["score"]["max_error"]), _number(loss))
