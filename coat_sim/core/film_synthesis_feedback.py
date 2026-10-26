"""Run-owned immutable audit guides; raw spectra and active errors stay distinct.

Pointwise failures add sparse wavelengths. Mean failures add the complete dense
band for that angle, so no worst wavelength is invented for an integral failure.
The parent owns optical work, budgets, cancellation and independent acceptance.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import math
from typing import TYPE_CHECKING, Any, Callable, Mapping

from .film_optimization import FilmOptimizationObjective
from .film_objective_criteria import reduce_objective_responses

if TYPE_CHECKING:
    from .film_auto_synthesis import FilmSynthesisRequest


@dataclass(frozen=True)
class FeedbackPoint:
    objective_index: int
    wavelength_nm: float | None
    angle_deg: float
    polarization: str
    reduction: str = "pointwise"
    wavelength_grid_nm: tuple[float, ...] = ()


@dataclass(frozen=True)
class FeedbackSnapshot:
    revision: int
    points: tuple[FeedbackPoint, ...]
    objectives: tuple[FilmOptimizationObjective, ...]
    acceptable_normalized_error: float


def _field(error: Any, name: str) -> Any:
    return error[name] if isinstance(error, Mapping) else getattr(error, name)


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("feedback condition requires finite numeric values")
    return float(value)


def _isolated_objective(objective, reduction, *, wave=None, angle=None, samples=None):
    changes = dict(pointwise=replace(objective.pointwise, enabled=reduction == "pointwise"),
                   mean=replace(objective.mean, enabled=reduction == "mean"))
    if wave is not None:
        changes.update(wavelength_min_nm=wave, wavelength_max_nm=wave, wavelength_samples=1)
    if angle is not None:
        changes['angles_deg'] = (angle,)
    if samples is not None:
        changes['wavelength_samples'] = samples
    return replace(objective, **changes)


def _checked_error(error, objectives):
    index = _field(error, 'objective_index')
    if type(index) is not int or not 0 <= index < len(objectives):
        raise ValueError('feedback objective index is invalid')
    objective = objectives[index]
    reduction = _field(error, 'reduction')
    if reduction not in ('pointwise', 'mean'):
        raise ValueError('feedback reduction is invalid')
    criterion = getattr(objective, reduction)
    if not criterion.enabled:
        raise ValueError('feedback condition uses a disabled constraint')
    angle = _number(_field(error, 'angle_deg'))
    wave = _field(error, 'wavelength_nm')
    if (angle not in objective.angles_deg
            or any(_field(error, key) != getattr(objective, key) for key in ('polarization', 'metric'))
            or any(_field(error, key) != getattr(criterion, key)
                   for key in ('target_value', 'lower_bound', 'upper_bound', 'strict_lower', 'strict_upper'))):
        raise ValueError('feedback condition differs from its original objective')
    if reduction == 'pointwise':
        wave = _number(wave)
        if not objective.wavelength_min_nm <= wave <= objective.wavelength_max_nm:
            raise ValueError('feedback wavelength differs from its original objective')
    elif wave is not None:
        raise ValueError('mean feedback cannot identify a worst wavelength')
    if (_field(error, 'wavelength_min_nm') != objective.wavelength_min_nm
            or _field(error, 'wavelength_max_nm') != objective.wavelength_max_nm):
        raise ValueError('feedback condition differs from its original band')
    actual = _number(_field(error, 'actual'))
    # A singleton physical value checks only the selected constraint. Full-band
    # averaging is separately verified against coverage before mean admission.
    test_wave = objective.wavelength_min_nm if wave is None else wave
    isolated = _isolated_objective(objective, reduction, wave=test_wave, angle=angle)
    reduced = reduce_objective_responses((isolated,), (dict(objective_index=0, angle_deg=angle,
        wavelength_nm=[test_wave], actual_values=[actual]),))
    normalized = reduced.max_error
    if not math.isclose(_number(_field(error, 'normalized_violation')), normalized,
                        rel_tol=1e-10, abs_tol=1e-12):
        raise ValueError('feedback violation differs from its original objective')
    return FeedbackPoint(index, wave, angle, objective.polarization, reduction), actual, normalized, reduced.nominal_pass


def _same_wave(left: float, right: float) -> bool:
    return abs(left - right) <= 4 * max(math.ulp(left), math.ulp(right))


def _on_training_grid(point, objective):
    if point.reduction != 'pointwise':
        return False
    low, high, count = objective.wavelength_min_nm, objective.wavelength_max_nm, objective.wavelength_samples
    if count == 1 or low == high:
        return _same_wave(point.wavelength_nm, low)
    index = round((point.wavelength_nm - low) / (high - low) * (count - 1))
    return _same_wave(point.wavelength_nm, low + (high - low) * index / (count - 1))


def _point_key(point):
    return (point.objective_index, point.angle_deg, point.polarization, point.reduction,
            0. if point.wavelength_nm is None else point.wavelength_nm, point.wavelength_grid_nm)


class SparseFeedbackRegistry:
    """Admit active failed conditions only after the parent's complete audit."""

    def __init__(self, request: FilmSynthesisRequest):
        self._objectives = tuple(request.objectives)
        self._acceptable = request.acceptable_normalized_error
        self._points: set[FeedbackPoint] = set()
        self._revision = 0

    def export_state(self):
        return dict(revision=self._revision,
                    points=[asdict(p) for p in sorted(self._points, key=_point_key)])

    def restore_state(self, state):
        """Restore only guides for the already fingerprint-checked frozen task."""
        if (not isinstance(state, Mapping) or set(state) != {'revision', 'points'}
                or not isinstance(state['points'], (list, tuple))
                or type(state.get('revision')) is not int or state['revision'] < 0):
            raise ValueError('invalid checkpoint audit feedback revision')
        points = set()
        for row in state.get('points', ()):
            data = dict(row)
            data['wavelength_grid_nm'] = tuple(data.get('wavelength_grid_nm', ()))
            point = FeedbackPoint(**data)
            if type(point.objective_index) is not int or not 0 <= point.objective_index < len(self._objectives):
                raise ValueError('invalid checkpoint feedback objective')
            objective = self._objectives[point.objective_index]
            if (point.reduction not in ('pointwise', 'mean')
                    or not getattr(objective, point.reduction).enabled
                    or point.angle_deg not in objective.angles_deg or point.polarization != objective.polarization):
                raise ValueError('checkpoint feedback changed original condition')
            low, high = objective.wavelength_min_nm, objective.wavelength_max_nm
            if point.reduction == 'pointwise':
                wave = _number(point.wavelength_nm)
                if not low <= wave <= high or point.wavelength_grid_nm:
                    raise ValueError('checkpoint point feedback differs from original band')
            else:
                grid = point.wavelength_grid_nm
                if (point.wavelength_nm is not None or not grid or grid[0] != low or grid[-1] != high
                        or any(not low <= _number(w) <= high for w in grid)
                        or any(a >= b for a, b in zip(grid, grid[1:], strict=False))):
                    raise ValueError('checkpoint mean feedback needs a complete ordered band')
            points.add(point)
        self._points, self._revision = points, state['revision']

    def record_audit(self, score: Mapping[str, Any]) -> int:
        coverage = {(_field(row, 'objective_index'), _field(row, 'angle_deg')): row
                    for row in score.get('coverage', ())}
        worst: dict[tuple[int, float, str, str], tuple[tuple[bool, float, float], FeedbackPoint]] = {}
        for error in score['errors']:
            point, actual, normalized, passed = _checked_error(error, self._objectives)
            if passed:
                continue
            objective = self._objectives[point.objective_index]
            if point.reduction == 'mean':
                raw = coverage.get((point.objective_index, point.angle_deg))
                if raw is None:
                    raise ValueError('mean audit feedback requires complete band coverage')
                waves = tuple(_field(raw, 'wavelength_nm'))
                isolated = _isolated_objective(objective, 'mean', angle=point.angle_deg, samples=len(waves))
                reduced = reduce_objective_responses((isolated,), (dict(objective_index=0,
                    angle_deg=point.angle_deg, wavelength_nm=waves, actual_values=_field(raw, 'actual_values')),))
                if not math.isclose(reduced.conditions[0].actual, actual, rel_tol=1e-12, abs_tol=1e-14):
                    raise ValueError('mean feedback differs from its complete band integral')
                point = replace(point, wavelength_grid_nm=waves)
            elif _on_training_grid(point, objective):
                continue
            if any(old.objective_index == point.objective_index and old.angle_deg == point.angle_deg
                   and old.polarization == point.polarization and old.reduction == point.reduction
                   and (old.wavelength_grid_nm == point.wavelength_grid_nm if point.reduction == 'mean'
                        else old.wavelength_nm is not None and point.wavelength_nm is not None
                        and _same_wave(old.wavelength_nm, point.wavelength_nm)) for old in self._points):
                continue
            group = (point.objective_index, point.angle_deg, point.polarization, point.reduction)
            criterion = getattr(objective, point.reduction)
            strict_failure = ((criterion.strict_lower and actual <= criterion.lower_bound)
                              or (criterion.strict_upper and actual >= criterion.upper_bound))
            severity = (strict_failure, normalized, -(point.wavelength_nm or 0.))
            if group not in worst or severity > worst[group][0]:
                worst[group] = severity, point
        additions = {entry[1] for entry in worst.values()} - self._points
        if additions:
            self._points.update(additions)
            self._revision += 1
        return len(additions)

    def snapshot(self) -> FeedbackSnapshot:
        return FeedbackSnapshot(self._revision, tuple(sorted(self._points, key=_point_key)),
                                self._objectives, self._acceptable)


def augment_feedback_guide(request: FilmSynthesisRequest, base: Mapping[str, Any],
                           snapshot: FeedbackSnapshot, *,
                           evaluate_points: Callable[[tuple[FilmOptimizationObjective, ...]], Mapping[str, Any]]) -> dict[str, Any]:
    """Keep original band responses intact and reduce supplemental guides apart.

    Auxiliary singleton pointwise bands or complete dense mean bands retain their
    own quadrature. They never alter original indices, training scores or export.
    """
    if (tuple(request.objectives) != snapshot.objectives
            or request.acceptable_normalized_error != snapshot.acceptable_normalized_error):
        raise ValueError('feedback snapshot belongs to different objective conditions')
    if '_feedback_base' in base:
        raise ValueError('a feedback guide cannot be augmented twice')
    if not base.get('responses') and snapshot.points:
        raise ValueError('feedback refinement requires complete base responses')
    base_reduction = reduce_objective_responses(request.objectives, base.get('responses', ()),
                                               tolerance=request.acceptable_normalized_error)
    guide = deepcopy(dict(base))
    maximum, failed = base_reduction.max_error, not base_reduction.nominal_pass
    if snapshot.points:
        objective_list: list[FilmOptimizationObjective] = []
        for point in snapshot.points:
            if type(point.objective_index) is not int or not 0 <= point.objective_index < len(snapshot.objectives):
                raise ValueError('feedback snapshot objective index is invalid')
            objective = snapshot.objectives[point.objective_index]
            if (point.reduction not in ('pointwise', 'mean')
                    or not getattr(objective, point.reduction).enabled
                    or point.angle_deg not in objective.angles_deg or point.polarization != objective.polarization):
                raise ValueError('feedback snapshot point differs from its original objective')
            if point.reduction == 'pointwise':
                wave = _number(point.wavelength_nm)
                if not objective.wavelength_min_nm <= wave <= objective.wavelength_max_nm or point.wavelength_grid_nm:
                    raise ValueError('feedback snapshot point has an invalid wavelength')
                isolated = _isolated_objective(objective, 'pointwise', wave=wave, angle=point.angle_deg)
            else:
                if point.wavelength_nm is not None or not point.wavelength_grid_nm:
                    raise ValueError('mean feedback requires its complete band grid')
                isolated = _isolated_objective(objective, 'mean', angle=point.angle_deg,
                                               samples=len(point.wavelength_grid_nm))
            objective_list.append(isolated)
        objectives: tuple[FilmOptimizationObjective, ...] = tuple(objective_list)
        extra = evaluate_points(objectives)
        if extra['design'] != base['design']:
            raise ValueError('feedback response belongs to another design')
        responses = tuple(extra['responses'])
        reduced = reduce_objective_responses(objectives, responses, tolerance=request.acceptable_normalized_error)
        if len(reduced.conditions) != len(objectives):
            raise ValueError('feedback evaluation is missing reduced conditions')
        for condition in reduced.conditions:
            point = snapshot.points[condition.objective_index]
            raw = next(row for row in reduced.coverage if row.objective_index == condition.objective_index)
            expected = (point.wavelength_nm,) if point.reduction == 'pointwise' else point.wavelength_grid_nm
            if raw.wavelength_nm != expected:
                raise ValueError('feedback response differs from its admitted audit grid')
        guide['_feedback_response_groups'] = ((objectives, responses),)
        maximum, failed = max(maximum, reduced.max_error), failed or not reduced.nominal_pass
    guide['score']['max_error'] = maximum
    guide['_feedback_base'] = base
    guide['_feedback_revision'] = snapshot.revision
    guide['_feedback_strict_failed'] = failed
    return guide


def restore_feedback_base(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the exact base value before archival or export."""
    return value.get('_feedback_base', value)


def feedback_candidate_key(value: Mapping[str, Any], loss: float) -> tuple[int, float, float]:
    """Nominal active conditions precede numerical guide loss within a snapshot."""
    return (int(bool(value.get('_feedback_strict_failed', False))),
            _number(value['score']['max_error']), _number(loss))
