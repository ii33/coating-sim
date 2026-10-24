"""Shared band constraints, physical reductions and their analytic derivatives.

Optical evaluation belongs to callers. This module only consumes complete raw
angle spectra, so wavelength averaging always precedes the constraint hinge.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from typing import Any, Iterable, Literal, Mapping, Sequence

import numpy as np

from .film_optimization_scoring import score_condition


@dataclass(frozen=True)
class FilmObjectiveConstraint:
    enabled: bool = True
    mode: Literal["value", "range"] = "range"
    target_value: float | None = None
    lower_bound: float | None = 0.0
    upper_bound: float | None = 1.0
    strict_lower: bool = False
    strict_upper: bool = False


@dataclass(frozen=True)
class FilmConditionError:
    objective_index: int
    wavelength_nm: float | None
    angle_deg: float
    polarization: str
    metric: str
    actual: float
    target_value: float | None
    lower_bound: float | None
    upper_bound: float | None
    normalized_violation: float
    weighted_normalized_violation: float
    reduction: Literal["mean", "pointwise"] = "pointwise"
    wavelength_min_nm: float | None = None
    wavelength_max_nm: float | None = None
    strict_lower: bool = False
    strict_upper: bool = False


@dataclass(frozen=True)
class FilmObjectiveCoverage:
    """Raw evidence, independent of the number of enabled reduced conditions."""

    objective_index: int
    angle_deg: float
    wavelength_nm: tuple[float, ...]
    actual_values: tuple[float, ...]


@dataclass(frozen=True)
class FilmObjectiveReduction:
    conditions: tuple[FilmConditionError, ...]
    residual_vector: np.ndarray
    jacobian_matrix: np.ndarray
    condition_masses: np.ndarray
    signed_bound_values: np.ndarray
    signed_bound_jacobian: np.ndarray
    signed_bound_masses: np.ndarray
    signed_bound_condition_indices: tuple[int, ...]
    coverage: tuple[FilmObjectiveCoverage, ...]
    max_error: float
    weighted_max_error: float
    weighted_rms_error: float
    nominal_pass: bool
    solver_acceptable: bool
    condition_log_masses: np.ndarray
    signed_bound_log_masses: np.ndarray


def _field(value: Any, name: str) -> Any:
    return value[name] if isinstance(value, Mapping) else getattr(value, name)


def _finite(value: Any, name: str) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _interval(constraint: Any) -> tuple[float, float, bool, bool]:
    if _field(constraint, "mode") == "value":
        target = _finite(_field(constraint, "target_value"), "target_value")
        return target, target, False, False
    lower, upper = _field(constraint, "lower_bound"), _field(constraint, "upper_bound")
    return (0.0 if lower is None else float(lower),
            1.0 if upper is None else float(upper),
            bool(_field(constraint, "strict_lower")),
            bool(_field(constraint, "strict_upper")))


def validate_objective_criteria(objective: Any) -> None:
    """Validate enabled constraints and their common physical feasible interval.

    Disabled rows have no numerical or acceptance effect. A pointwise interval
    also bounds its mean, hence disjoint mean/pointwise intervals are impossible.
    """
    active = []
    for reduction in ("mean", "pointwise"):
        constraint = _field(objective, reduction)
        if type(_field(constraint, "enabled")) is not bool:
            raise ValueError(f"{reduction}.enabled must be boolean")
        if not _field(constraint, "enabled"):
            continue
        mode = _field(constraint, "mode")
        if mode not in ("value", "range"):
            raise ValueError(f"{reduction}.mode must be value or range")
        for name in ("strict_lower", "strict_upper"):
            if type(_field(constraint, name)) is not bool:
                raise ValueError(f"{reduction}.{name} must be boolean")
        if mode == "value":
            if (_field(constraint, "lower_bound") is not None
                    or _field(constraint, "upper_bound") is not None):
                raise ValueError("value constraints cannot retain range bounds")
            target = _finite(_field(constraint, "target_value"), "target_value")
            if not 0 <= target <= 1:
                raise ValueError("target_value must be between 0 and 1")
            if _field(constraint, "strict_lower") or _field(constraint, "strict_upper"):
                raise ValueError("value targets are closed points and cannot be strict")
        else:
            if _field(constraint, "target_value") is not None:
                raise ValueError("range constraints cannot retain a value target")
            lower, upper = _field(constraint, "lower_bound"), _field(constraint, "upper_bound")
            if lower is None and upper is None:
                raise ValueError("range constraints need at least one bound")
            for name, bound in (("lower_bound", lower), ("upper_bound", upper)):
                if bound is not None and not 0 <= _finite(bound, name) <= 1:
                    raise ValueError(f"{name} must be between 0 and 1")
            if ((_field(constraint, "strict_lower") and lower is None)
                    or (_field(constraint, "strict_upper") and upper is None)):
                raise ValueError("strict flags need their corresponding bound")
        active.append(_interval(constraint))
    if not active:
        raise ValueError("at least one of mean and pointwise must be enabled")
    low = max(row[0] for row in active)
    high = min(row[1] for row in active)
    strict_low = any(row[0] == low and row[2] for row in active)
    strict_high = any(row[1] == high and row[3] for row in active)
    if low > high or (low == high and (strict_low or strict_high)):
        raise ValueError("enabled mean/pointwise constraints have an empty intersection")
    for name in ("weight", "normalization_scale"):
        if _finite(_field(objective, name), name) <= 0:
            raise ValueError(f"{name} must be positive")


def active_constraints(objective: Any) -> tuple[tuple[Literal["mean", "pointwise"], Any], ...]:
    """Enabled reductions in their canonical mean, then pointwise order."""
    names: tuple[Literal["mean", "pointwise"], ...] = ("mean", "pointwise")
    return tuple((name, _field(objective, name)) for name in names
                 if _field(_field(objective, name), "enabled"))


def signed_bound_count(objectives: Sequence[Any]) -> int:
    """Signed rows on the objectives' declared complete sampling grids."""
    count = 0
    for objective in objectives:
        for name, constraint in active_constraints(objective):
            sides = (2 if _field(constraint, "mode") == "value" else
                     int(_field(constraint, "lower_bound") is not None)
                     + int(_field(constraint, "upper_bound") is not None))
            samples = 1 if name == "mean" else _field(objective, "wavelength_samples")
            count += len(_field(objective, "angles_deg")) * samples * sides
    return count


def normalized_band_weights(objectives: Sequence[Any]) -> np.ndarray:
    """Stable band-only diagnostic masses, independent of wavelength counts."""
    if not objectives:
        raise ValueError("at least one objective is required")
    weights = [_finite(_field(objective, "weight"), "weight") for objective in objectives]
    if any(weight <= 0 for weight in weights):
        raise ValueError("weight must be positive")
    largest = max(weights)
    relative = np.asarray([weight / largest for weight in weights])
    result = relative / math.fsum(relative)
    result.setflags(write=False)
    return result


def wavelength_measure(wavelengths: Sequence[float] | np.ndarray,
                       lower_nm: float, upper_nm: float) -> np.ndarray:
    """Normalized nonuniform trapezoid masses, or unit mass for a single line.

    Endpoints must equal the requested endpoints exactly. Caller-side matching
    of simulator coordinates must happen before constructing these raw frames.
    """
    low, high = _finite(lower_nm, "wavelength minimum"), _finite(upper_nm, "wavelength maximum")
    waves: np.ndarray = np.asarray(wavelengths, dtype=float)
    if waves.ndim != 1 or not len(waves) or not np.all(np.isfinite(waves)) or high < low:
        raise ValueError("invalid wavelength grid")
    if waves[0] != low or waves[-1] != high:
        raise ValueError("wavelength grid must contain the exact requested endpoints")
    if low == high:
        if len(waves) != 1:
            raise ValueError("single-line objectives need exactly one sample")
        return np.ones(1)
    if len(waves) < 2 or np.any(np.diff(waves) <= 0):
        raise ValueError("positive-width bands need at least two strictly increasing samples")
    width = high - low
    if not math.isfinite(width):
        raise ValueError("wavelength band width must be finite")
    q = np.empty(len(waves))
    q[0], q[-1] = (waves[1] - waves[0]) / width / 2, (waves[-1] - waves[-2]) / width / 2
    q[1:-1] = ((waves[2:] - waves[:-2]) / width) / 2
    if not np.all(np.isfinite(q)) or np.any(q <= 0):
        raise ValueError("wavelength measure must have positive finite masses")
    return q


def _scaled_mass(value: float, scale: float, weight: float,
                 largest_weight: float, divisor: float, measure: float = 1.0) -> float:
    """value/scale * sqrt(weight/largest_weight/divisor), without unsafe ratios."""
    if value == 0:
        return 0.0
    vm, ve = math.frexp(value)
    sm, se = math.frexp(scale)
    wm, we = math.frexp(weight)
    lm, le = math.frexp(largest_weight)
    qm, qe = math.frexp(measure)
    root_exp, odd = divmod(we - le + qe, 2)
    root = math.sqrt(wm / lm * qm * (2.0 if odd else 1.0) / divisor)
    mantissa, shift = math.frexp(vm / sm * root)
    try:
        result = math.ldexp(mantissa, ve - se + root_exp + shift)
    except OverflowError as exc:
        raise ValueError("weighted residual or Jacobian is not finite") from exc
    if not math.isfinite(result):
        raise ValueError("weighted residual or Jacobian is not finite")
    return result


def reduce_objective_responses(
    objectives: Sequence[Any], responses: Iterable[Mapping[str, Any]], *,
    layer_ids: Sequence[str] = (), tolerance: float = 0.0,
) -> FilmObjectiveReduction:
    """Reduce raw complete angle spectra once for every solver and audit path.

    Frames may be contiguous chunks, in increasing wavelength order per angle.
    Jacobian dictionaries map stable layer IDs to one derivative per wavelength.
    Residuals and their Jacobians include the square root of normalized band
    mass; signed bound values and their Jacobians never include that mass.
    """
    if not objectives:
        raise ValueError("at least one objective is required")
    tolerance = _finite(tolerance, "tolerance")
    if tolerance < 0 or len(set(layer_ids)) != len(layer_ids):
        raise ValueError("invalid tolerance or duplicate derivative layer IDs")
    for objective in objectives:
        validate_objective_criteria(objective)
    weights = [float(_field(row, "weight")) for row in objectives]
    largest = max(weights)
    total_relative_weight = math.fsum(weight / largest for weight in weights)
    expected: set[tuple[int, float]] = set()
    for index, objective in enumerate(objectives):
        angles = tuple(_finite(angle, "angle_deg") for angle in _field(objective, "angles_deg"))
        if not angles or len(set(angles)) != len(angles):
            raise ValueError("objectives need nonempty unique angles")
        expected.update((index, angle) for angle in angles)
    grouped: dict[tuple[int, float], list[Mapping[str, Any]]] = {}
    for frame in responses:
        index = _field(frame, "objective_index")
        angle = _finite(_field(frame, "angle_deg"), "angle_deg")
        if type(index) is not int or (index, angle) not in expected:
            raise ValueError("unexpected objective or angle in raw response")
        grouped.setdefault((index, angle), []).append(frame)
    if set(grouped) != expected:
        raise ValueError("missing complete objective/angle response")
    conditions: list[FilmConditionError] = []
    residuals, jacobians, masses = [], [], []
    bounds, bound_jacobians, bound_masses, bound_indices = [], [], [], []
    coverage, log_masses, bound_log_masses = [], [], []
    nominal_pass = acceptable = True
    max_error = weighted_max = 0.0
    for index, objective in enumerate(objectives):
        low, high = float(_field(objective, "wavelength_min_nm")), float(_field(objective, "wavelength_max_nm"))
        active = active_constraints(objective)
        scale, weight = float(_field(objective, "normalization_scale")), weights[index]
        divisor = total_relative_weight * len(_field(objective, "angles_deg")) * len(active)
        for angle in _field(objective, "angles_deg"):
            wave_parts, value_parts, jacobian_parts = [], [], []
            for frame in grouped[index, float(angle)]:
                waves = np.asarray(_field(frame, "wavelength_nm"), dtype=float)
                values = np.asarray(_field(frame, "actual_values"), dtype=float)
                if (waves.ndim != 1 or not len(waves) or values.shape != waves.shape
                        or not np.all(np.isfinite(values))):
                    raise ValueError("invalid or missing raw optical samples")
                if layer_ids:
                    derivatives = _field(frame, "jacobian_values_by_layer_id")
                    columns = [np.asarray(derivatives[layer], dtype=float) for layer in layer_ids]
                    if any(column.shape != waves.shape for column in columns):
                        raise ValueError("incomplete raw optical Jacobian")
                    jacobian = np.column_stack(columns)
                    if not np.all(np.isfinite(jacobian)):
                        raise ValueError("raw optical Jacobian must be finite")
                else:
                    jacobian = np.empty((len(waves), 0))
                wave_parts.append(waves)
                value_parts.append(values)
                jacobian_parts.append(jacobian)
            waves = wave_parts[0] if len(wave_parts) == 1 else np.concatenate(wave_parts)
            values = value_parts[0] if len(value_parts) == 1 else np.concatenate(value_parts)
            jacobian = jacobian_parts[0] if len(jacobian_parts) == 1 else np.concatenate(jacobian_parts)
            q = wavelength_measure(waves, low, high)
            coverage.append(FilmObjectiveCoverage(index, float(angle), tuple(map(float, waves)), tuple(map(float, values))))
            for name, constraint in active:
                rows: Iterable[tuple[float | None, float, np.ndarray, float]]
                if name == "mean":
                    rows = [(None, float(q @ values), q @ jacobian, 1.0)]
                else:
                    rows = zip(waves, values, jacobian, q, strict=True)
                for wave, actual, derivative, measure in rows:
                    actual = float(actual)
                    mode = _field(constraint, "mode")
                    lower, upper = _field(constraint, "lower_bound"), _field(constraint, "upper_bound")
                    target = _field(constraint, "target_value")
                    if mode == "value":
                        signed = actual - target
                        hinge_derivative = derivative
                        signed_rows = [(signed / scale, derivative / scale), (-signed / scale, -derivative / scale)]
                        passed = actual == target
                    else:
                        clipped = min(math.inf if upper is None else upper,
                                      max(-math.inf if lower is None else lower, actual))
                        signed = actual - clipped
                        hinge_derivative = derivative if signed != 0 else np.zeros(len(layer_ids))
                        signed_rows = []
                        if lower is not None:
                            signed_rows.append(((lower - actual) / scale, -derivative / scale))
                        if upper is not None:
                            signed_rows.append(((actual - upper) / scale, derivative / scale))
                        passed = ((lower is None or (actual > lower if _field(constraint, "strict_lower") else actual >= lower))
                                  and (upper is None or (actual < upper if _field(constraint, "strict_upper") else actual <= upper)))
                    normalized, weighted = score_condition(abs(signed), scale, weight)
                    strict_ok = ((not _field(constraint, "strict_lower") or actual > lower)
                                 and (not _field(constraint, "strict_upper") or actual < upper))
                    nominal_pass &= passed
                    acceptable &= normalized <= tolerance and strict_ok
                    max_error, weighted_max = max(max_error, normalized), max(weighted_max, weighted)
                    measure = float(measure)
                    mass_root = _scaled_mass(1.0, 1.0, weight, largest, divisor, measure)
                    mass = mass_root * mass_root
                    log_mass = math.log(weight) - math.log(largest) - math.log(divisor) + math.log(measure)
                    condition_index = len(conditions)
                    conditions.append(FilmConditionError(
                        index, None if wave is None else float(wave), float(angle),
                        _field(objective, "polarization"), _field(objective, "metric"), actual,
                        target, lower, upper, normalized, weighted, name, low, high,
                        _field(constraint, "strict_lower"), _field(constraint, "strict_upper")))
                    masses.append(mass)
                    log_masses.append(log_mass)
                    residuals.append(_scaled_mass(signed, scale, weight, largest, divisor, measure))
                    jacobians.append([_scaled_mass(float(value), scale, weight, largest, divisor, measure)
                                      for value in hinge_derivative])
                    for bound, bound_derivative in signed_rows:
                        bounds.append(bound)
                        bound_jacobians.append(bound_derivative)
                        bound_masses.append(mass / len(signed_rows))
                        bound_log_masses.append(log_mass - math.log(len(signed_rows)))
                        bound_indices.append(condition_index)
    residual_vector = np.asarray(residuals)
    jacobian_matrix = (np.asarray(jacobians).reshape((-1, len(layer_ids)))
                       if layer_ids else np.empty((len(conditions), 0)))
    condition_masses = np.asarray(masses)
    signed_bound_values = np.asarray(bounds)
    signed_bound_jacobian = (np.asarray(bound_jacobians).reshape((-1, len(layer_ids)))
                            if layer_ids else np.empty((len(bounds), 0)))
    signed_bound_masses = np.asarray(bound_masses)
    arrays = [residual_vector, jacobian_matrix, condition_masses, signed_bound_values,
              signed_bound_jacobian, signed_bound_masses]
    if any(not np.all(np.isfinite(array)) for array in arrays):
        raise ValueError("reduced optical criteria must be finite")
    condition_log_masses = np.asarray(log_masses)
    signed_bound_log_masses = np.asarray(bound_log_masses)
    logs = [condition_log_masses, signed_bound_log_masses]
    for array in arrays + logs:
        array.setflags(write=False)
    return FilmObjectiveReduction(
        conditions=tuple(conditions), residual_vector=residual_vector,
        jacobian_matrix=jacobian_matrix, condition_masses=condition_masses,
        signed_bound_values=signed_bound_values, signed_bound_jacobian=signed_bound_jacobian,
        signed_bound_masses=signed_bound_masses, signed_bound_condition_indices=tuple(bound_indices),
        coverage=tuple(coverage), max_error=max_error, weighted_max_error=weighted_max,
        weighted_rms_error=math.hypot(*residuals), nominal_pass=bool(nominal_pass),
        solver_acceptable=bool(acceptable), condition_log_masses=condition_log_masses,
        signed_bound_log_masses=signed_bound_log_masses)
