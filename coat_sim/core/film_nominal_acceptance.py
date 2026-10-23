"""Original optical bounds, distinct from solver tolerances and search guides.

No margin or approximate equality is applied to physical values. Coordinate
roundoff is allowed only when identifying a required sampled wavelength.
"""
from __future__ import annotations

from bisect import bisect_left
import math
from typing import Any, Mapping

ACCEPTANCE_SEMANTICS = "nominal_bounds_v1"


def _field(value: Any, name: str) -> Any:
    return value[name] if isinstance(value, Mapping) else getattr(value, name)


def nominal_condition_pass(objective: Any, actual: float, *, strict_lower: bool = False,
                           strict_upper: bool = False) -> bool:
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
        return bool((lower is None or (actual > lower if strict_lower else actual >= lower))
                    and (upper is None or (actual < upper if strict_upper else actual <= upper)))
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


def solver_condition_acceptable(objective: Any, actual: float, tolerance: float, *,
                                strict_lower: bool = False, strict_upper: bool = False) -> bool:
    """Keep the requested normalized numerical tolerance as a separate metric."""
    if isinstance(actual, bool) or not math.isfinite(actual):
        return False
    lower, upper = _field(objective, "lower_bound"), _field(objective, "upper_bound")
    if ((strict_lower and (lower is None or actual <= lower))
            or (strict_upper and (upper is None or actual >= upper))):
        return False
    violation = (abs(actual - _field(objective, "target_value"))
                 if _field(objective, "mode") == "value" else
                 max(0., 0. if lower is None else lower - actual,
                     0. if upper is None else actual - upper))
    return bool(violation / _field(objective, "normalization_scale") <= tolerance)


def _has_wave(waves: list[float], expected: float) -> bool:
    cursor = bisect_left(waves, expected)
    return any(abs(wave - expected) <= 4 * max(math.ulp(wave), math.ulp(expected))
               for wave in waves[max(0, cursor - 1):cursor + 1])


def complete_original_score(request: Any, score: Any, *, independent_audit: bool = False) -> bool:
    """Require original training grid or a completed dense/offset/extremum audit.

    This is evidence-shape validation, not physical replay. Sparse feedback
    guides cannot pass: every original objective/angle/grid point is required.
    Historical claims additionally require the caller's frozen physical replay.
    """
    if not isinstance(score, Mapping) or not isinstance(score.get("errors"), (tuple, list)):
        return False
    try:
        groups: dict[tuple[int, float], list[float]] = {}
        identities = set()
        maximum = 0.0
        for row in score["errors"]:
            index = _field(row, "objective_index")
            if type(index) is not int or not 0 <= index < len(request.objectives):
                return False
            objective = request.objectives[index]
            wave, angle, actual = (_field(row, key) for key in ("wavelength_nm", "angle_deg", "actual"))
            if (any(isinstance(value, bool) or not math.isfinite(value) for value in (wave, angle, actual))
                    or not objective.wavelength_min_nm <= wave <= objective.wavelength_max_nm
                    or angle not in objective.angles_deg
                    or any(_field(row, key) != getattr(objective, key) for key in
                           ("polarization", "metric", "target_value", "lower_bound", "upper_bound"))):
                return False
            identity = (index, angle, wave)
            if identity in identities:
                return False
            identities.add(identity)
            groups.setdefault((index, angle), []).append(wave)
            violation = (abs(actual - objective.target_value) if objective.mode == "value" else
                max(0., 0. if objective.lower_bound is None else objective.lower_bound - actual,
                    0. if objective.upper_bound is None else actual - objective.upper_bound))
            normalized = violation / objective.normalization_scale
            saved = _field(row, "normalized_violation")
            if (isinstance(saved, bool) or not math.isfinite(saved)
                    or not math.isclose(saved, normalized, rel_tol=1e-10, abs_tol=1e-12)):
                return False
            maximum = max(maximum, normalized)
        saved_maximum = score.get("max_error")
        if (not isinstance(saved_maximum, (int, float)) or isinstance(saved_maximum, bool)
                or not math.isfinite(saved_maximum)
                or saved_maximum < 0 or not math.isclose(saved_maximum, maximum, rel_tol=1e-10, abs_tol=1e-12)):
            return False
        dense_points = offset_points = 0
        for index, objective in enumerate(request.objectives):
            count = objective.wavelength_samples
            if independent_audit and count > 1:
                count = (count - 1) * request.validation_samples_multiplier + 1
            low, high = objective.wavelength_min_nm, objective.wavelength_max_nm
            for angle in objective.angles_deg:
                waves = sorted(groups.get((index, angle), ()))
                expected = [low] if count == 1 else [low + (high - low) * i / (count - 1) for i in range(count)]
                if not all(_has_wave(waves, wave) for wave in expected):
                    return False
                dense_points += count
                if independent_audit and count > 1 and high > low:
                    step = (high - low) / (count - 1)
                    # Match the audit's separately linspaced shifted endpoints.
                    shifted_low, shifted_high = low + .5 * step, high - .5 * step
                    offsets = ([shifted_low] if count == 2 else
                        [shifted_low + (shifted_high - shifted_low) * i / (count - 2) for i in range(count - 1)])
                    if not all(_has_wave(waves, wave) for wave in offsets):
                        return False
                    offset_points += count - 1
        if independent_audit:
            audit = score.get("audit")
            if (not isinstance(audit, Mapping) or audit.get("dense_points") != dense_points
                    or audit.get("offset_points") != offset_points
                    or audit.get("unique_condition_points") != len(identities)
                    or type(audit.get("bracket_count")) is not int or audit["bracket_count"] < 0
                    or audit.get("completed_extrema") != audit["bracket_count"]
                    or audit.get("continuous_certificate") is not False
                    or audit.get("rms_measure") != "fixed_dense_grid_only"):
                return False
        return True
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return False


def nominal_targets_pass(request: Any, score: Any, *, independent_audit: bool = False) -> bool:
    """True only for a complete original score satisfying nominal bounds."""
    return (complete_original_score(request, score, independent_audit=independent_audit)
            and all(nominal_condition_pass(request.objectives[_field(row, "objective_index")],
                _field(row, "actual"),
                strict_lower=_field(row, "objective_index") in request.strict_lower_bound_objective_indices,
                strict_upper=_field(row, "objective_index") in request.strict_upper_bound_objective_indices)
                for row in score["errors"]))


def solver_acceptable(request: Any, score: Any, *, independent_audit: bool = False) -> bool:
    """True only for a complete score within the user's original tolerance."""
    return (complete_original_score(request, score, independent_audit=independent_audit)
            and all(solver_condition_acceptable(request.objectives[_field(row, "objective_index")],
                _field(row, "actual"), request.acceptable_normalized_error,
                strict_lower=_field(row, "objective_index") in request.strict_lower_bound_objective_indices,
                strict_upper=_field(row, "objective_index") in request.strict_upper_bound_objective_indices)
                for row in score["errors"]))
