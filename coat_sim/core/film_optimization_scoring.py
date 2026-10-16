"""Finite and numerically stable score primitives for film optimization."""
from __future__ import annotations

import math
from typing import Iterable


def _finite_float(value: float, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _weighted_scaled_value(value: float, scale: float, weight: float) -> float:
    magnitude = abs(value)
    if magnitude == 0.0:
        return 0.0
    vm, ve = math.frexp(magnitude)
    sm, se = math.frexp(scale)
    wm, we = math.frexp(weight)
    we2, odd = divmod(we, 2)
    root_mantissa = math.sqrt(wm * (2.0 if odd else 1.0))
    mantissa, adjustment = math.frexp(vm / sm * root_mantissa)
    try:
        result = math.ldexp(mantissa, ve - se + we2 + adjustment)
    except OverflowError:
        result = math.inf
    if value < 0:
        result = -result
    if not math.isfinite(result):
        raise ValueError("weighted normalized error is not finite")
    return result


def weighted_scaled_value(value: float, normalization_scale: float,
                          weight: float) -> float:
    """Compute sqrt(weight) * value / scale without an unsafe product."""
    raw = _finite_float(value, "value")
    scale = _finite_float(normalization_scale, "normalization_scale")
    condition_weight = _finite_float(weight, "weight")
    if scale <= 0:
        raise ValueError("normalization_scale must be positive")
    if condition_weight <= 0:
        raise ValueError("weight must be positive")
    return _weighted_scaled_value(raw, scale, condition_weight)


def score_condition(
    violation: float,
    normalization_scale: float,
    weight: float,
) -> tuple[float, float]:
    """Return normalized and weighted error, rejecting nonfinite metrics."""
    raw = _finite_float(violation, "violation")
    scale = _finite_float(normalization_scale, "normalization_scale")
    condition_weight = _finite_float(weight, "weight")
    if raw < 0:
        raise ValueError("violation must be nonnegative")
    if scale <= 0:
        raise ValueError("normalization_scale must be positive")
    if condition_weight <= 0:
        raise ValueError("weight must be positive")
    normalized = raw / scale
    if not math.isfinite(normalized):
        raise ValueError("normalized error is not finite")
    weighted = _weighted_scaled_value(raw, scale, condition_weight)
    return normalized, weighted


def weighted_rms_error(
    normalized_errors: Iterable[float],
    weights: Iterable[float],
) -> float:
    """Compute sqrt(sum(w*e**2)/sum(w)) using binary exponents."""
    errors = tuple(_finite_float(value, "normalized error") for value in normalized_errors)
    condition_weights = tuple(_finite_float(value, "weight") for value in weights)
    if not errors or len(errors) != len(condition_weights):
        raise ValueError("normalized errors and weights must be nonempty and have equal length")
    if any(value < 0 for value in errors):
        raise ValueError("normalized errors must be nonnegative")
    if any(value <= 0 for value in condition_weights):
        raise ValueError("weights must be positive")

    products: list[tuple[float, int]] = []
    weight_parts: list[tuple[float, int]] = []
    for error, weight in zip(errors, condition_weights):
        wm, we = math.frexp(weight)
        root_exp, odd = divmod(we, 2)
        root_mantissa = math.sqrt(wm * (2.0 if odd else 1.0))
        weight_parts.append((wm, we))
        if error == 0.0:
            continue
        em, ee = math.frexp(error)
        pm, adjust = math.frexp(em * root_mantissa)
        products.append((pm, ee + root_exp + adjust))

    if not products:
        return 0.0
    max_product_exp = max(exp for _mant, exp in products)
    scaled_products = [math.ldexp(mant, exp - max_product_exp)
                       for mant, exp in products]
    numerator = math.fsum(value * value for value in scaled_products)
    max_weight_exp = max(exp for _mant, exp in weight_parts)
    scaled_weights = [math.ldexp(mant, exp - max_weight_exp)
                      for mant, exp in weight_parts]
    denominator = math.fsum(scaled_weights)
    if (not math.isfinite(numerator) or not math.isfinite(denominator)
            or numerator <= 0 or denominator <= 0):
        raise ValueError("weighted RMS accumulation is not finite")
    result_exp = 2 * max_product_exp - max_weight_exp
    result_shift, odd = divmod(result_exp, 2)
    scaled_result = math.sqrt((numerator / denominator) * (2.0 if odd else 1.0))
    try:
        result = math.ldexp(scaled_result, result_shift)
    except OverflowError as exc:
        raise ValueError("weighted RMS error is not finite") from exc
    if not math.isfinite(result):
        raise ValueError("weighted RMS error is not finite")
    return result


class PreparedConditionScore:
    """Reuse validated invariant scale/weight, retaining the original arithmetic."""

    def __init__(self, scale: float, weight: float):
        self.scale = _finite_float(scale, "normalization_scale")
        weight = _finite_float(weight, "weight")
        if self.scale <= 0 or weight <= 0:
            raise ValueError("normalization_scale and weight must be positive")
        self.unit = self.scale == 1.0 and weight == 1.0
        self.sm, self.se = math.frexp(self.scale)
        wm, we = math.frexp(weight)
        self.we2, odd = divmod(we, 2)
        self.root_mantissa = math.sqrt(wm * (2.0 if odd else 1.0))

    def __call__(self, violation: float) -> tuple[float, float]:
        raw = _finite_float(violation, "violation")
        if raw < 0:
            raise ValueError("violation must be nonnegative")
        if self.unit:
            return raw, (0.0 if raw == 0 else raw)
        if raw == 0:
            return raw / self.scale, 0.0
        normalized = raw / self.scale
        if not math.isfinite(normalized):
            raise ValueError("normalized error is not finite")
        vm, ve = math.frexp(raw)
        mantissa, adjustment = math.frexp(vm / self.sm * self.root_mantissa)
        try:
            weighted = math.ldexp(mantissa, ve - self.se + self.we2 + adjustment)
        except OverflowError:
            weighted = math.inf
        if not math.isfinite(weighted):
            raise ValueError("weighted normalized error is not finite")
        return normalized, weighted


class PreparedRmsWeights:
    """Freeze the weight decomposition; do not change frexp/fsum RMS reduction."""

    def __init__(self, weights: Iterable[float]):
        weights = tuple(_finite_float(w, "weight") for w in weights)
        if not weights or any(w <= 0 for w in weights):
            raise ValueError("weights must be nonempty and positive")
        parts = tuple(math.frexp(w) for w in weights)
        self.roots = []
        for wm, we in parts:
            shift, odd = divmod(we, 2)
            self.roots.append((math.sqrt(wm * (2.0 if odd else 1.0)), shift))
        self.max_weight_exp = max(exp for _, exp in parts)
        self.denominator = math.fsum(math.ldexp(m, e - self.max_weight_exp) for m, e in parts)

    def __call__(self, normalized_errors: Iterable[float]) -> float:
        errors = tuple(_finite_float(e, "normalized error") for e in normalized_errors)
        if len(errors) != len(self.roots) or any(e < 0 for e in errors):
            raise ValueError("normalized errors must be nonnegative and match weights")
        products = []
        for error, (root_mantissa, root_exp) in zip(errors, self.roots):
            if error == 0:
                continue
            em, ee = math.frexp(error)
            pm, adjust = math.frexp(em * root_mantissa)
            products.append((pm, ee + root_exp + adjust))
        if not products:
            return 0.0
        max_exp = max(e for _, e in products)
        scaled = [math.ldexp(m, e - max_exp) for m, e in products]
        numerator = math.fsum(v * v for v in scaled)
        if not math.isfinite(numerator) or numerator <= 0:
            raise ValueError("weighted RMS accumulation is not finite")
        shift, odd = divmod(2 * max_exp - self.max_weight_exp, 2)
        scaled_result = math.sqrt((numerator / self.denominator) * (2.0 if odd else 1.0))
        try:
            result = math.ldexp(scaled_result, shift)
        except OverflowError as exc:
            raise ValueError("weighted RMS error is not finite") from exc
        if not math.isfinite(result):
            raise ValueError("weighted RMS error is not finite")
        return result
