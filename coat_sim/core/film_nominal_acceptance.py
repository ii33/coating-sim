"""Original optical bounds, distinct from solver tolerances and search guides.

No margin or approximate equality is applied to physical values. Coordinate
roundoff is allowed only when identifying a required sampled wavelength.
"""
from __future__ import annotations

from bisect import bisect_left
import math
from typing import Any, Mapping

ACCEPTANCE_SEMANTICS = "band_criteria_v2"


def _field(value: Any, name: str) -> Any:
    return value[name] if isinstance(value, Mapping) else getattr(value, name)


def nominal_condition_pass(objective: Any, actual: float, *, strict_lower: bool | None = None,
                           strict_upper: bool | None = None) -> bool:
    """Test one actual physical response against its exact original target.

    Unflagged range bounds are inclusive. Explicit strict flags select > or <,
    never a safety margin. Value targets require equality, not solver tolerance.
    """
    if isinstance(actual, bool):
        return False
    try:
        if not math.isfinite(actual):
            return False
        if _field(objective, "mode") == "value":
            return bool(actual == _field(objective, "target_value"))
        lower, upper = _field(objective, "lower_bound"), _field(objective, "upper_bound")
        strict_lower = _field(objective, "strict_lower") if strict_lower is None else strict_lower
        strict_upper = _field(objective, "strict_upper") if strict_upper is None else strict_upper
        return bool((lower is None or (actual > lower if strict_lower else actual >= lower))
                    and (upper is None or (actual < upper if strict_upper else actual <= upper)))
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


def solver_condition_acceptable(objective: Any, actual: float, tolerance: float, *,
                                normalization_scale: float = 1.0,
                                strict_lower: bool | None = None, strict_upper: bool | None = None) -> bool:
    """Keep the requested normalized numerical tolerance as a separate metric."""
    if isinstance(actual, bool) or not math.isfinite(actual):
        return False
    lower, upper = _field(objective, "lower_bound"), _field(objective, "upper_bound")
    strict_lower = _field(objective, "strict_lower") if strict_lower is None else strict_lower
    strict_upper = _field(objective, "strict_upper") if strict_upper is None else strict_upper
    if ((strict_lower and (lower is None or actual <= lower))
            or (strict_upper and (upper is None or actual >= upper))):
        return False
    violation = (abs(actual - _field(objective, "target_value"))
                 if _field(objective, "mode") == "value" else
                 max(0., 0. if lower is None else lower - actual,
                     0. if upper is None else actual - upper))
    return bool(violation / normalization_scale <= tolerance)


def _has_wave(waves: list[float], expected: float) -> bool:
    cursor = bisect_left(waves, expected)
    return any(abs(wave - expected) <= 4 * max(math.ulp(wave), math.ulp(expected))
               for wave in waves[max(0, cursor - 1):cursor + 1])


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _close(a: Any, b: float) -> bool:
    return _number(a) and math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-12)


def _coverage_groups(objectives: Any, coverage: Any) -> dict:
    """Validate physical evidence without inferring constraints from its rows."""
    if not isinstance(coverage, (tuple, list)):
        raise ValueError("complete raw coverage is required")
    groups = {}
    for row in coverage:
        index, angle = _field(row, "objective_index"), _field(row, "angle_deg")
        if type(index) is not int or not 0 <= index < len(objectives):
            raise ValueError("invalid coverage objective")
        objective = objectives[index]
        if not _number(angle) or angle not in objective.angles_deg or (index, angle) in groups:
            raise ValueError("invalid or duplicate coverage angle")
        waves, actual = tuple(_field(row, "wavelength_nm")), tuple(_field(row, "actual_values"))
        if (not waves or len(waves) != len(actual) or not all(_number(x) for x in waves + actual)
                or any(b <= a for a, b in zip(waves, waves[1:], strict=False))
                or waves[0] < objective.wavelength_min_nm or waves[-1] > objective.wavelength_max_nm):
            raise ValueError("invalid raw spectral coverage")
        groups[index, angle] = (waves, actual)
    if set(groups) != {(i, a) for i, o in enumerate(objectives) for a in o.angles_deg}:
        raise ValueError("missing raw objective/angle coverage")
    return groups


def _coverage_responses(coverage: Any) -> tuple:
    return tuple({name: _field(row, name) for name in
                  ("objective_index", "angle_deg", "wavelength_nm", "actual_values")}
                 for row in coverage)


def complete_original_score(request: Any, score: Any, *, independent_audit: bool = False) -> bool:
    """Require complete raw grids and exactly the enabled constraint records.

    Means are recomputed only on the declared dense grid. Auxiliary audit
    points provide pointwise evidence and never acquire integration weight.
    This validates evidence shape and arithmetic, not physical replay.
    """
    if not isinstance(score, Mapping) or not isinstance(score.get("errors"), (tuple, list)):
        return False
    try:
        from .film_objective_criteria import FilmConditionError, reduce_objective_responses

        groups = _coverage_groups(request.objectives, score.get("coverage"))
        dense_points = offset_points = 0
        for index, objective in enumerate(request.objectives):
            count = objective.wavelength_samples
            if independent_audit and count > 1:
                count = (count - 1) * request.validation_samples_multiplier + 1
            low, high = objective.wavelength_min_nm, objective.wavelength_max_nm
            expected = [low] if count == 1 else [low + (high - low) * i / (count - 1) for i in range(count)]
            for angle in objective.angles_deg:
                waves, _actual = groups[index, angle]
                if len(waves) != count or not all(_has_wave(list(waves), wave) for wave in expected):
                    return False
                dense_points += count
        reduction = reduce_objective_responses(request.objectives, _coverage_responses(score["coverage"]),
                                               tolerance=request.acceptable_normalized_error)
        expected_rows: dict[tuple[int, float, str, float | None], FilmConditionError | None] = {
            (e.objective_index, e.angle_deg, e.reduction, e.wavelength_nm): e
            for e in reduction.conditions}
        raw_groups = groups
        if independent_audit:
            raw_groups = _coverage_groups(request.objectives, score.get("audit_coverage"))
            for (index, angle), (waves, actuals) in raw_groups.items():
                objective = request.objectives[index]
                dense_waves, dense_actual = groups[index, angle]
                for wave, actual in zip(dense_waves, dense_actual, strict=True):
                    pos = bisect_left(waves, wave)
                    if pos == len(waves) or waves[pos] != wave or actuals[pos] != actual:
                        return False
                count = len(dense_waves)
                if count > 1 and objective.wavelength_max_nm > objective.wavelength_min_nm:
                    low, high = objective.wavelength_min_nm, objective.wavelength_max_nm
                    step = (high - low) / (count - 1)
                    offsets = [low + .5 * step] if count == 2 else [
                        low + .5 * step + (high - low - step) * i / (count - 2) for i in range(count - 1)]
                    if not all(_has_wave(list(waves), wave) for wave in offsets):
                        return False
                    offset_points += count - 1
                if objective.pointwise.enabled:
                    for wave in waves:
                        expected_rows.setdefault((index, angle, "pointwise", wave), None)
        seen = set()
        maximum = weighted_maximum = 0.0
        for row in score["errors"]:
            index, angle, kind, wave = (_field(row, n) for n in
                                       ("objective_index", "angle_deg", "reduction", "wavelength_nm"))
            if type(index) is not int or not _number(angle):
                return False
            identity = index, angle, kind, wave
            if identity not in expected_rows or identity in seen:
                return False
            seen.add(identity)
            objective = request.objectives[index]
            constraint = getattr(objective, kind)
            if not constraint.enabled:
                return False
            canonical = expected_rows[identity]
            actual = (_field(canonical, "actual") if canonical is not None else
                      raw_groups[index, angle][1][raw_groups[index, angle][0].index(wave)])
            if not _number(_field(row, "actual")) or _field(row, "actual") != actual:
                return False
            for name in ("polarization", "metric", "wavelength_min_nm", "wavelength_max_nm"):
                if _field(row, name) != getattr(objective, name):
                    return False
            for name in ("target_value", "lower_bound", "upper_bound", "strict_lower", "strict_upper"):
                value = _field(row, name)
                if ((name.startswith("strict_") and type(value) is not bool)
                        or (not name.startswith("strict_") and value is not None and not _number(value))
                        or value != getattr(constraint, name)):
                    return False
            violation = (abs(actual - constraint.target_value) if constraint.mode == "value" else
                         max(0., 0. if constraint.lower_bound is None else constraint.lower_bound - actual,
                             0. if constraint.upper_bound is None else actual - constraint.upper_bound))
            normalized = violation / objective.normalization_scale
            weighted = normalized * math.sqrt(objective.weight)
            if (not _close(_field(row, "normalized_violation"), normalized)
                    or not _close(_field(row, "weighted_normalized_violation"), weighted)):
                return False
            maximum, weighted_maximum = max(maximum, normalized), max(weighted_maximum, weighted)
        if (seen != set(expected_rows) or not _close(score.get("max_error"), maximum)
                or not _close(score.get("weighted_rms_error"), reduction.weighted_rms_error)):
            return False
        if "weighted_max_error" in score and not _close(score["weighted_max_error"], weighted_maximum):
            return False
        nominal = all(nominal_condition_pass(getattr(request.objectives[_field(row, "objective_index")],
            _field(row, "reduction")), _field(row, "actual")) for row in score["errors"])
        acceptable = all(solver_condition_acceptable(
            getattr(request.objectives[_field(row, "objective_index")], _field(row, "reduction")),
            _field(row, "actual"), request.acceptable_normalized_error,
            normalization_scale=request.objectives[_field(row, "objective_index")].normalization_scale)
            for row in score["errors"])
        if (("nominal_pass" in score and score["nominal_pass"] is not nominal)
                or ("solver_acceptable" in score and score["solver_acceptable"] is not acceptable)
                or ("condition_count" in score and (type(score["condition_count"]) is not int
                    or score["condition_count"] != len(score["errors"])))):
            return False
        if independent_audit:
            audit = score.get("audit")
            if (not isinstance(audit, Mapping) or audit.get("dense_points") != dense_points
                    or audit.get("offset_points") != offset_points
                    or audit.get("unique_condition_points") != sum(len(w) for w, _a in raw_groups.values())
                    or type(audit.get("physical_evaluations")) is not int or audit["physical_evaluations"] < 1
                    or type(audit.get("wavelength_points")) is not int
                    or audit["wavelength_points"] < audit["unique_condition_points"]
                    or type(audit.get("bracket_count")) is not int or audit["bracket_count"] < 0
                    or audit.get("completed_extrema") != audit["bracket_count"]
                    or audit.get("continuous_certificate") is not False
                    or audit.get("rms_measure") != "fixed_dense_grid_only"):
                return False
        return True
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError, IndexError):
        return False


def nominal_targets_pass(request: Any, score: Any, *, independent_audit: bool = False) -> bool:
    """AND of enabled exact criteria, after validating full physical coverage."""
    return (complete_original_score(request, score, independent_audit=independent_audit)
            and all(nominal_condition_pass(getattr(request.objectives[_field(row, "objective_index")],
                _field(row, "reduction")), _field(row, "actual")) for row in score["errors"]))


def solver_acceptable(request: Any, score: Any, *, independent_audit: bool = False) -> bool:
    """AND of enabled solver tolerances, separately from exact nominal pass."""
    return (complete_original_score(request, score, independent_audit=independent_audit)
            and all(solver_condition_acceptable(getattr(request.objectives[_field(row, "objective_index")],
                _field(row, "reduction")), _field(row, "actual"), request.acceptable_normalized_error,
                normalization_scale=request.objectives[_field(row, "objective_index")].normalization_scale)
                for row in score["errors"]))
