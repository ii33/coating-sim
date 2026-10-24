"""Strict current-wire band criteria; no legacy objective translation."""
from __future__ import annotations

from dataclasses import asdict, fields
import math
from numbers import Real
from typing import Any, Mapping

from .film_objective_criteria import (
    FilmConditionError, FilmObjectiveConstraint, FilmObjectiveCoverage, validate_objective_criteria,
)


def constraint_from_payload(value: Any) -> FilmObjectiveConstraint:
    expected = {f.name for f in fields(FilmObjectiveConstraint)}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("current constraint requires exactly its enabled, mode, bounds and strict fields")
    data = dict(value)
    if any(type(data[name]) is not bool for name in ("enabled", "strict_lower", "strict_upper")):
        raise ValueError("constraint flags must be boolean")
    if data["mode"] not in ("range", "value"):
        raise ValueError("constraint mode must be range or value")
    return FilmObjectiveConstraint(**data)


def objective_from_payload(value: Any):
    # Local import: the optimizer re-exports the shared condition type.
    from .film_optimization import FilmOptimizationObjective

    expected = {f.name for f in fields(FilmOptimizationObjective)}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("current band objective requires complete shared fields and mean/pointwise constraints")
    data = dict(value)
    if not isinstance(data["angles_deg"], (tuple, list)):
        raise ValueError("angles_deg must be an array")
    data["angles_deg"] = tuple(data["angles_deg"])
    for name in ("mean", "pointwise"):
        data[name] = constraint_from_payload(data[name])
    for name in ("wavelength_min_nm", "wavelength_max_nm"):
        _finite_number(data[name], name)
    low, high, count = data["wavelength_min_nm"], data["wavelength_max_nm"], data["wavelength_samples"]
    if low <= 0 or high < low or type(count) is not int or count < 1:
        raise ValueError("band requires positive ordered wavelengths and an integer sample count")
    if (low == high and count != 1) or (low < high and count < 2):
        raise ValueError("single lines require one sample; positive-width bands require at least two")
    if not data["angles_deg"]:
        raise ValueError("band needs at least one angle")
    for angle in data["angles_deg"]:
        _finite_number(angle, "angle_deg")
        if not 0 <= angle < 90:
            raise ValueError("band angles must be in [0, 90) degrees")
    if len(set(data["angles_deg"])) != len(data["angles_deg"]):
        raise ValueError("band angles cannot repeat")
    if data["metric"] not in ("R", "T") or data["polarization"] not in ("s", "p", "unpolarized"):
        raise ValueError("band supports R/T and s/p/unpolarized only")
    objective = FilmOptimizationObjective(**data)
    validate_objective_criteria(objective)
    return objective


def objective_to_payload(objective: Any) -> dict[str, Any]:
    validate_objective_criteria(objective)
    payload = asdict(objective)
    payload["angles_deg"] = list(objective.angles_deg)
    return payload


def condition_from_payload(value: Any) -> FilmConditionError:
    expected = {f.name for f in fields(FilmConditionError)}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("current condition requires reduction and original band range")
    condition = FilmConditionError(**dict(value))
    if condition.reduction not in ("mean", "pointwise"):
        raise ValueError("condition reduction must be mean or pointwise")
    if (condition.reduction == "mean") != (condition.wavelength_nm is None):
        raise ValueError("only mean conditions have no single wavelength")
    if type(condition.objective_index) is not int or condition.objective_index < 0:
        raise ValueError("condition objective_index must be a nonnegative integer")
    if any(type(getattr(condition, name)) is not bool for name in ("strict_lower", "strict_upper")):
        raise ValueError("condition strict flags must be boolean")
    if condition.metric not in ("R", "T") or condition.polarization not in ("s", "p", "unpolarized"):
        raise ValueError("condition metric or polarization is unsupported")
    for name in ("angle_deg", "actual", "normalized_violation", "weighted_normalized_violation",
                 "wavelength_min_nm", "wavelength_max_nm"):
        _finite_number(getattr(condition, name), name)
    for name in ("wavelength_nm", "target_value", "lower_bound", "upper_bound"):
        value = getattr(condition, name)
        if value is not None:
            _finite_number(value, name)
    return condition


def coverage_from_payload(value: Any) -> FilmObjectiveCoverage:
    expected = {f.name for f in fields(FilmObjectiveCoverage)}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("raw coverage requires objective, angle and complete wavelength/response arrays")
    data = dict(value)
    for name in ("wavelength_nm", "actual_values"):
        if not isinstance(data[name], (tuple, list)):
            raise ValueError("raw coverage arrays are required")
        data[name] = tuple(data[name])
    if type(data["objective_index"]) is not int or data["objective_index"] < 0:
        raise ValueError("raw coverage objective_index must be a nonnegative integer")
    _finite_number(data["angle_deg"], "angle_deg")
    if not data["wavelength_nm"] or len(data["wavelength_nm"]) != len(data["actual_values"]):
        raise ValueError("raw coverage arrays must be nonempty and equally sized")
    for name in ("wavelength_nm", "actual_values"):
        for number in data[name]:
            _finite_number(number, name)
    return FilmObjectiveCoverage(**data)


def _finite_number(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
