"""Parity checks for the precomputed film-scoring callables."""

from __future__ import annotations

import math
import random

import pytest

from coat_sim.core.film_optimization_scoring import (
    PreparedConditionScore,
    PreparedRmsWeights,
    score_condition,
    weighted_rms_error,
)


def _outcome(call, *args):
    try:
        return ("value", call(*args))
    except Exception as exc:  # Compare the same failure path, including overflow.
        return ("error", type(exc), str(exc))


def _assert_same_outcome(left, right):
    expected = _outcome(*left)
    actual = _outcome(*right)
    assert actual == expected


def _positive_float(rng: random.Random) -> float:
    """Make a finite positive float with a seeded, broad binary exponent."""
    exponent = rng.randint(-1073, 1023)
    mantissa = rng.uniform(0.5, math.nextafter(1.0, 0.0))
    result = math.ldexp(mantissa, exponent)
    assert math.isfinite(result) and result > 0.0
    return result


def test_prepared_condition_score_exactly_matches_reference():
    rng = random.Random(0xC0A7)
    scales = [
        1.0,
        0.25,
        3.0,
        1e-200,
        1e200,
        math.ulp(0.0),
        math.nextafter(0.0, 1.0),
        math.nextafter(1.0, 0.0),
        float.fromhex("0x1.fffffffffffffp+1023"),
    ]
    weights = [
        1.0,
        0.125,
        7.0,
        1e-250,
        1e250,
        math.ulp(0.0),
        math.nextafter(1.0, 0.0),
        float.fromhex("0x1.fffffffffffffp+1023"),
    ]
    violations = [
        0.0,
        1.0,
        0.125,
        1e-250,
        1e250,
        math.ulp(0.0),
        math.nextafter(1.0, 0.0),
        float.fromhex("0x1.fffffffffffffp+1023"),
    ]
    scales.extend(_positive_float(rng) for _ in range(12))
    weights.extend(_positive_float(rng) for _ in range(12))
    violations.extend(_positive_float(rng) for _ in range(12))

    for scale in scales:
        for weight in weights:
            prepared = PreparedConditionScore(scale, weight)
            for violation in violations:
                _assert_same_outcome(
                    (score_condition, violation, scale, weight),
                    (prepared, violation),
                )


def test_prepared_rms_weights_exactly_match_reference():
    rng = random.Random(0x51A7)
    edge_values = [
        0.0,
        1.0,
        0.125,
        1e-250,
        1e250,
        math.ulp(0.0),
        math.nextafter(1.0, 0.0),
        float.fromhex("0x1.fffffffffffffp+1023"),
    ]
    edge_weights = [
        1.0,
        0.125,
        7.0,
        1e-250,
        1e250,
        math.ulp(0.0),
        math.nextafter(1.0, 0.0),
        float.fromhex("0x1.fffffffffffffp+1023"),
    ]

    cases = [
        ([0.0], [1.0]),
        ([1.0], [1.0]),
        (edge_values, edge_weights),
        ([float.fromhex("0x1.fffffffffffffp+1023"), math.ulp(0.0)],
         [math.ulp(0.0), float.fromhex("0x1.fffffffffffffp+1023")]),
    ]
    for length in (1, 2, 5, 13):
        errors = [0.0 if i == 0 else _positive_float(rng) for i in range(length)]
        weights = [_positive_float(rng) for _ in range(length)]
        cases.append((errors, weights))

    for errors, weights in cases:
        prepared = PreparedRmsWeights(weights)
        _assert_same_outcome(
            (weighted_rms_error, errors, weights),
            (prepared, errors),
        )


@pytest.mark.parametrize("bad_value", [True, False, math.nan, math.inf, -math.inf, -1.0])
def test_prepared_condition_score_rejects_invalid_call_values(bad_value):
    prepared = PreparedConditionScore(1.5, 2.0)
    with pytest.raises(ValueError):
        score_condition(bad_value, 1.5, 2.0)
    with pytest.raises(ValueError):
        prepared(bad_value)


@pytest.mark.parametrize("bad_value", [True, False, math.nan, math.inf, -math.inf, -1.0])
def test_prepared_condition_score_rejects_invalid_invariants(bad_value):
    for constructor_args in ((bad_value, 1.0), (1.0, bad_value)):
        with pytest.raises(ValueError):
            PreparedConditionScore(*constructor_args)
        with pytest.raises(ValueError):
            score_condition(1.0, *constructor_args)


@pytest.mark.parametrize("bad_value", [True, False, math.nan, math.inf, -math.inf, -1.0])
def test_prepared_rms_weights_reject_invalid_weights(bad_value):
    with pytest.raises(ValueError):
        PreparedRmsWeights([bad_value])
    with pytest.raises(ValueError):
        weighted_rms_error([1.0], [bad_value])


@pytest.mark.parametrize("bad_value", [True, False, math.nan, math.inf, -math.inf, -1.0])
def test_prepared_rms_weights_reject_invalid_call_values(bad_value):
    prepared = PreparedRmsWeights([1.0])
    with pytest.raises(ValueError):
        prepared([bad_value])
    with pytest.raises(ValueError):
        weighted_rms_error([bad_value], [1.0])


@pytest.mark.parametrize("weights", [[], [0.0], [-1.0]])
def test_prepared_rms_weights_reject_empty_or_nonpositive_weights(weights):
    with pytest.raises(ValueError):
        PreparedRmsWeights(weights)


@pytest.mark.parametrize(
    "errors,weights",
    [([], []), ([], [1.0]), ([1.0], []), ([1.0, 2.0], [1.0])],
)
def test_prepared_rms_length_validation_matches_reference(errors, weights):
    with pytest.raises(ValueError):
        weighted_rms_error(errors, weights)
    # Empty weights are rejected while preparing; for nonempty weights the
    # prepared callable rejects the same invalid length at call time.
    if weights:
        scorer = PreparedRmsWeights(weights)
        with pytest.raises(ValueError):
            scorer(errors)
    else:
        with pytest.raises(ValueError):
            PreparedRmsWeights(weights)
