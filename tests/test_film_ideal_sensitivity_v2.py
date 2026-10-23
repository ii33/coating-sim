from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from importlib import import_module
from typing import Any, cast
import hashlib
import json
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from coat_sim.core import film_ideal_sensitivity as v2
from coat_sim.core import _film_ideal_sensitivity_core as core
from coat_sim.core.film_optimization import FilmOptimizationRequest
from tests.test_film_ideal_sensitivity import problem

# Exercise the canonical current schema2 public API.
ideal = cast(Any, import_module("coat_sim.core.film_ideal_sensitivity"))

pytestmark = pytest.mark.algorithm


def request_v2(*, delta=0.1, policy="bounded_baseline_degradation", **kwargs):
    service, ordinary = problem(**kwargs)
    ids = tuple(f"objective-{i}" for i in range(len(ordinary.objectives)))
    return service, replace(
        ordinary,
        policy=policy,
        objective_ids=ids,
        budgets=tuple(v2.FilmIdealDegradationBudget(i, delta) for i in ids)
        if policy == "bounded_baseline_degradation"
        else (),
    )


def evaluated(service, request):
    frozen = ideal.freeze_film_ideal_sensitivity_request(service, request)
    evaluator = core._Evaluator(frozen)
    data = evaluator.evaluate(frozen.design_snapshot)
    evidence = core._baseline_evidence(frozen, data)
    return frozen, evaluator, data, evidence


def substituted(data, values):
    return replace(data, values=np.asarray(values, dtype=float))


def test_real_nominal_failed_initial_can_start_and_improve_and_replay():
    service, request = request_v2(upper=0.001)
    frozen = ideal.freeze_film_ideal_sensitivity_request(service, request)
    before = ideal.film_ideal_sensitivity_request_to_payload(frozen)
    result = ideal.execute_film_ideal_sensitivity_request(None, frozen)
    assert result.improved and result.usable_candidate
    assert result.baseline_analysis.nominal_targets_met is False
    assert result.best_analysis.objective_value < result.baseline_analysis.objective_value
    assert result.baseline_guard.guard_pass and result.best_guard.guard_pass
    assert ideal.film_ideal_sensitivity_request_to_payload(result.frozen_request) == before
    payload = ideal.film_ideal_sensitivity_result_to_payload(result)
    assert payload["schema_version"] == 2 and payload["policy"] == request.policy
    assert (
        ideal.validate_film_ideal_sensitivity_result_payload(
            payload, expected_request=frozen, replay=True
        )
        == payload
    )


def test_real_spectral_worsening_inside_allowance_is_usable_not_nominal_met():
    service, request = request_v2(upper=0.001)
    design = deepcopy(request.design_snapshot)
    design["front_layers"][0]["thickness"]["value"] = 180.0
    request = replace(
        request,
        design_snapshot=design,
        variables=(replace(request.variables[0], lower_nm=130.0, upper_nm=230.0),),
    )
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.usable_candidate and result.improved
    old = result.baseline_analysis.nominal_conditions[0].actual
    new = result.best_analysis.nominal_conditions[0].actual
    assert old < new <= old + 0.001
    assert result.best_analysis.nominal_targets_met is False
    assert result.nominal_targets_met is False and result.targets_met is False
    row = result.best_guard.rows[0]
    assert row.delta_fraction == 0.001 and row.delta_percentage_point == 0.1
    assert 0 < row.consumed_fraction <= 0.001 and row.excess_fraction == 0.0
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(result),
        expected_request=result.frozen_request,
    )


@pytest.mark.parametrize(
    "mode,lower,upper,lower_side,upper_side",
    [
        ("range", 0.1, None, True, False),
        ("range", None, 0.3, False, True),
        ("range", 0.1, 0.3, True, True),
        ("value", None, None, True, True),
    ],
)
@pytest.mark.parametrize("delta", [0.0, 0.1])
def test_closed_pointwise_directional_guard_and_zero_allowance(
    mode, lower, upper, lower_side, upper_side, delta
):
    service, request = request_v2(delta=delta)
    objective = replace(
        request.objectives[0],
        mode=mode,
        lower_bound=lower,
        upper_bound=upper,
        target_value=0.2 if mode == "value" else None,
    )
    request = replace(request, objectives=(objective,))
    frozen, _ev, data, evidence = evaluated(service, request)
    y0 = data.values[0]
    d = delta / 100
    assert core._guard(data, frozen, evidence).guard_pass
    assert core._guard(substituted(data, [y0 - d]), frozen, evidence).guard_pass
    assert core._guard(substituted(data, [y0 + d]), frozen, evidence).guard_pass
    below = substituted(data, [np.nextafter(y0 - d, -np.inf)])
    above = substituted(data, [np.nextafter(y0 + d, np.inf)])
    assert core._guard(below, frozen, evidence).guard_pass is (not lower_side)
    assert core._guard(above, frozen, evidence).guard_pass is (not upper_side)
    if lower_side:
        assert core._guard(below, frozen, evidence).rows[0].excess_fraction > 0
    if upper_side:
        assert core._guard(above, frozen, evidence).rows[0].excess_fraction > 0
    rows = core._constraint_rows(data, frozen, [0], np.array([85.0]), evidence)
    assert len(rows[0]) == int(lower_side) + int(upper_side)
    assert len(rows[2]) == 0  # Bilateral zero/value guard remains inequalities.


def test_overlapping_rows_cannot_average_or_compensate_and_budgets_follow_ids():
    service, request = request_v2()
    objectives = (
        replace(request.objectives[0], upper_bound=0.01),
        replace(request.objectives[0], lower_bound=0.02, upper_bound=None),
    )
    request = replace(
        request,
        objectives=objectives,
        objective_ids=("upper-row", "lower-row"),
        budgets=(
            v2.FilmIdealDegradationBudget("lower-row", 0.2),
            v2.FilmIdealDegradationBudget("upper-row", 0.1),
        ),
    )
    frozen, _ev, data, evidence = evaluated(service, request)
    changed = data.values.copy()
    changed[0] += 0.0011
    changed[1] += 0.02  # Large improvement on the other objective cannot offset it.
    guard = core._guard(substituted(data, changed), frozen, evidence)
    assert not guard.guard_pass
    assert guard.rows[0].delta_fraction == 0.001
    assert guard.rows[1].delta_fraction == 0.002
    assert guard.rows[0].excess_fraction > 0
    reordered = replace(
        request, objectives=objectives[::-1], objective_ids=request.objective_ids[::-1]
    )
    f2, _e2, d2, b2 = evaluated(service, reordered)
    assert [r.delta_fraction for r in core._guard(d2, f2, b2).rows] == [0.002, 0.001]


def test_baseline_guard_never_rolls_forward():
    service, request = request_v2()
    frozen, _ev, data, evidence = evaluated(service, request)
    first = substituted(data, data.values + 0.00075)
    second = substituted(data, data.values + 0.0015)
    assert core._guard(first, frozen, evidence).guard_pass
    assert not core._guard(second, frozen, evidence).guard_pass
    assert evidence.values == tuple(data.values)


@pytest.mark.parametrize("change", ["missing", "nan", "negative", "unit", "duplicate", "unknown"])
def test_budget_contract_rejects_missing_nonfinite_ambiguous_or_wrong_units(change):
    _service, request = request_v2()
    b = request.budgets[0]
    budgets = {
        "missing": (),
        "nan": (replace(b, delta=float("nan")),),
        "negative": (replace(b, delta=-0.1),),
        "unit": (replace(b, unit="fraction"),),
        "duplicate": (b, b),
        "unknown": (replace(b, objective_id="not-the-row"),),
    }[change]
    with pytest.raises(ValueError):
        ideal.film_ideal_sensitivity_request_to_payload(replace(request, budgets=budgets))


@pytest.mark.parametrize("field", ["policy", "objective_ids", "budgets"])
def test_v2_parser_requires_explicit_policy_rows_and_budgets(field):
    _service, request = request_v2()
    payload = ideal.film_ideal_sensitivity_request_to_payload(request)
    payload.pop(field)
    with pytest.raises(ValueError, match="explicit"):
        ideal.film_ideal_sensitivity_request_from_payload(payload)


@pytest.mark.parametrize("field", ["unit", "delta", "objective_id"])
def test_v2_parser_requires_every_budget_field(field):
    _service, request = request_v2()
    payload = ideal.film_ideal_sensitivity_request_to_payload(request)
    payload["budgets"][0].pop(field)
    with pytest.raises(ValueError, match="explicit"):
        ideal.film_ideal_sensitivity_request_from_payload(payload)


def test_explicit_current_adapter_preserves_general_inputs_and_requires_policy_contract():
    _service, ordinary = problem()
    base = FilmOptimizationRequest(
        ordinary.design_snapshot, ordinary.design_revision, ordinary.variables, ordinary.objectives
    )
    request = ideal.ideal_sensitivity_request_from_optimization(
        base,
        sensitivity_mode="relative",
        policy="bounded_baseline_degradation",
        objective_ids=("stable-row",),
        budgets=(v2.FilmIdealDegradationBudget("stable-row", 0),),
    )
    assert type(request) is ideal.FilmIdealSensitivityRequest
    assert request.variables == ordinary.variables and request.sensitivity_mode == "relative"
    assert ideal.film_ideal_sensitivity_request_to_payload(request)["schema_version"] == 2
    with pytest.raises(TypeError):
        ideal.ideal_sensitivity_request_from_optimization(base, sensitivity_mode="absolute")


def test_current_hash_serialization_strict_nominal_and_schema1_rejection():
    service, request = request_v2(policy="strict_nominal", upper=0.001)
    payload = ideal.film_ideal_sensitivity_request_to_payload(request)
    expected = dict(kind=ideal.REQUEST_KIND, **asdict(ideal._validate(request)))
    assert payload == core._json_native(expected)
    identity = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()
    assert core._request_identity(request) == identity
    rejected = deepcopy(payload)
    rejected["schema_version"] = 1
    with pytest.raises(ValueError, match="schema"):
        ideal.film_ideal_sensitivity_request_from_payload(rejected)
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.termination_reason == "initial_nominal_infeasible"
    assert result.best_design is None and result.baseline_guard.guard_pass is False
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(result)
    )


@pytest.mark.parametrize("strict", [False, True])
def test_nominal_equality_remains_independent_from_closed_degradation_guard(strict):
    service, request = request_v2(delta=0.0)
    frozen, _ev, data, _baseline = evaluated(service, request)
    objective = replace(request.objectives[0], upper_bound=float(data.values[0]))
    request = replace(
        request,
        objectives=(objective,),
        strict_upper_bound_objective_indices=(0,) if strict else (),
    )
    result = ideal.analyze_film_ideal_sensitivity(service, request)
    assert result.baseline_analysis.nominal_targets_met is (not strict)
    assert result.baseline_guard.guard_pass
    assert result.targets_met is (not strict)


def test_full_grid_is_frozen_without_resampling_and_bound_to_bits_units_order():
    service, request = request_v2()
    request = replace(
        request,
        objectives=(
            replace(
                request.objectives[0],
                wavelength_min_nm=500,
                wavelength_max_nm=504,
                wavelength_samples=5,
                angles_deg=(0.0, 42.0),
            ),
        ),
    )
    result = ideal.analyze_film_ideal_sensitivity(service, request)
    evidence = result.baseline_evidence
    assert (
        len(evidence.values) == 10
        and evidence.dtype == "<f8"
        and evidence.spectral_unit == "fraction"
    )
    assert [c.wavelength_nm for c in evidence.conditions] == [500.0, 501.0, 502.0, 503.0, 504.0] * 2
    assert all(
        c.wavelength_bits == np.asarray(c.wavelength_nm, dtype="<f8").tobytes().hex()
        for c in evidence.conditions
    )
    payload = ideal.film_ideal_sensitivity_result_to_payload(result)
    for mutation in ("value", "order", "unit", "dtype", "digest", "budget", "kernel"):
        altered = deepcopy(payload)
        if mutation == "value":
            altered["baseline_evidence"]["values"][0] += 1e-7
        elif mutation == "order":
            altered["baseline_evidence"]["conditions"].reverse()
        elif mutation == "unit":
            altered["baseline_evidence"]["spectral_unit"] = "percentage_point"
        elif mutation == "dtype":
            altered["baseline_evidence"]["dtype"] = ">f8"
        elif mutation == "digest":
            altered["baseline_evidence"]["digest"] = "0" * 64
        elif mutation == "budget":
            altered["frozen_request"]["budgets"][0]["delta"] = 0.2
        else:
            altered["frozen_request"]["kernel_identity"] = "0" * 64
        with pytest.raises(ValueError):
            ideal.validate_film_ideal_sensitivity_result_payload(
                altered, expected_request=result.frozen_request
            )


def test_replay_catches_self_consistently_rehashed_forged_baseline():
    service, request = request_v2()
    result = ideal.analyze_film_ideal_sensitivity(service, request)
    evidence = replace(
        result.baseline_evidence, values=(result.baseline_evidence.values[0] + 1e-7,)
    )
    evidence = replace(evidence, digest=core._evidence_digest(evidence))
    altered = replace(result, baseline_evidence=evidence)
    with pytest.raises(ValueError, match="baseline"):
        ideal.validate_film_ideal_sensitivity_result_payload(
            ideal.film_ideal_sensitivity_result_to_payload(altered)
        )


def test_freeze_does_no_physical_forward(monkeypatch):
    service, request = request_v2()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("pre-submit physical forward")

    monkeypatch.setattr(core, "simulate_with_thickness_jacobian", forbidden)
    frozen = ideal.freeze_film_ideal_sensitivity_request(service, request)
    assert frozen.kernel_identity and not hasattr(frozen, "baseline_values")


@pytest.mark.parametrize("failure", ["budget", "cancel", "nonfinite"])
def test_incomplete_baseline_has_no_evidence_guard_or_candidate(monkeypatch, failure):
    service, request = request_v2(max_evaluations=1)
    request = replace(request, objectives=(replace(request.objectives[0], angles_deg=(0.0, 42.0)),))
    cancel = Event()
    original = core.simulate_with_thickness_jacobian

    def patched(*args, **kwargs):
        answer = original(*args, **kwargs)
        if failure == "cancel":
            cancel.set()
        if failure == "nonfinite":
            answer["R"] = np.full(len(answer["R"]), np.nan)
        return answer

    monkeypatch.setattr(core, "simulate_with_thickness_jacobian", patched)
    result = ideal.execute_film_ideal_sensitivity_request(service, request, cancel_event=cancel)
    assert result.baseline_analysis is None and result.baseline_evidence is None
    assert result.baseline_guard is None and result.best_guard is None
    assert not result.guard_pass and not result.usable_candidate and result.best_design is None
    assert result.forward_evaluations == result.jacobian_evaluations == 1
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(result)
    )


@pytest.mark.parametrize(
    "value,low,high",
    [
        (80.0, 60.0, 145.0),
        (60.0, 60.0, 145.0),
        (145.0, 60.0, 145.0),
        (80.0, np.nextafter(80.0, -np.inf), 145.0),
        (1.0, 1.0, 1.0 + 1e-9),
        (0.0, 0.0, 1.0),
    ],
)
def test_largest_legal_fd_template_actual_offsets_and_quadratic_derivative(value, low, high):
    x1, x2 = core._bounded_stencil(value, low, high)
    assert low <= x1 <= high and low <= x2 <= high and len({value, x1, x2}) == 3

    def f(x):
        return (x - value) ** 2 + 3 * (x - value) + 5

    assert core._three_point_derivative(f(value), f(x1), f(x2), value, x1, x2) == pytest.approx(
        3.0, rel=1e-6
    )
    if low == np.nextafter(80.0, -np.inf):
        assert x1 > value and x2 > x1
        # A near-wall central stencil would collapse to an ULP-sized step.
        # The current maximum-legal template instead uses two full right probes.
        hbase = np.cbrt(np.finfo(float).eps) * max(1.0, abs(value))
        assert x1 - value == pytest.approx(hbase, rel=1e-10)
        assert x2 - value == pytest.approx(2 * hbase, rel=1e-10)

        def smooth(x):
            return np.sin(x / 7.0)

        exact = np.cos(value / 7.0) / 7.0
        derivative = core._three_point_derivative(
            smooth(value), smooth(x1), smooth(x2), value, x1, x2)
        assert derivative == pytest.approx(exact, rel=1e-8)


@pytest.mark.parametrize("scale", [0.0, -1.0, float("nan"), float("inf")])
def test_fd_rejects_nonpositive_nonfinite_step_scales(scale):
    with pytest.raises(ValueError):
        core._bounded_stencil(80.0, 60.0, 145.0, step_scale=scale)


@pytest.mark.parametrize(
    "value,low,high",
    [(1.0, 1.0, 1.0), (1.0, 1.0, np.nextafter(1.0, np.inf)), (0.0, 0.0, np.nextafter(0.0, 1.0))],
)
def test_fd_no_independent_two_point_domain_is_explicit(value, low, high):
    with pytest.raises(core._FDDomainInsufficient, match="fd_domain_insufficient"):
        core._bounded_stencil(value, low, high)


@pytest.mark.parametrize("mode", ["absolute", "relative"])
def test_real_fd_h_half_and_conditioning_scale_preserve_same_F(mode):
    service, request = request_v2(mode=mode, two=True)
    frozen, evaluator, data, evidence = evaluated(service, request)
    scores = []
    for step_scale in (1.0, 0.5):
        derivative = []
        for variable in frozen.variables:
            value = evaluator.physical(frozen.design_snapshot)[variable.layer_id]
            x1, x2 = core._bounded_stencil(
                value, variable.lower_nm, variable.upper_nm, step_scale=step_scale
            )
            fs = [
                evaluator.evaluate(
                    core._candidate_design(frozen.design_snapshot, (variable,), (x,)), probe=True
                ).analysis.objective_value
                for x in (x1, x2)
            ]
            derivative.append(
                core._three_point_derivative(data.analysis.objective_value, fs[0], fs[1], value, x1, x2)
            )
        scores.append(derivative)
    np.testing.assert_allclose(scores[0], scores[1], rtol=3e-7, atol=1e-12)
    assert evaluator.probes == 8
    # Scaling all normalization scales affects raw F, not F/F0 or its derivatives.
    scaled = replace(
        request,
        objectives=tuple(
            replace(o, normalization_scale=o.normalization_scale * 10) for o in request.objectives
        ),
    )
    _f2, e2, d2, _b2 = evaluated(service, scaled)
    assert d2.analysis.objective_value == pytest.approx(
        data.analysis.objective_value / 100, rel=2e-14
    )


def test_constraint_cache_exact_z_identity_readonly_and_failed_build_eviction():
    cache = core._ConstraintRowsCache()
    builds = []
    z = np.array([0.25])
    source = np.ones((2, 1))

    def build():
        builds.append(1)
        return np.ones(2), source, np.empty(0), np.empty((0, 1))

    a = cache.get("same", z, build)
    for _ in range(4):
        assert cache.get("same", z.copy(), build) is a
    assert len(builds) == 1
    source[:] = 17.0
    assert np.all(a[1] == 1.0)
    with pytest.raises(ValueError):
        a[1][0, 0] = 4.0
    with pytest.raises(ValueError):
        a[1].setflags(write=True)
    b = cache.get("same", np.nextafter(z, np.inf), build)
    assert b is not a and len(builds) == 2
    cache.get("new-identity", z, build)
    assert len(builds) == 3

    def failing():
        raise ValueError("not complete")

    with pytest.raises(ValueError):
        cache.get("bad", z, failing)
    assert cache.key is None and cache.rows is None
    cache.get("new-identity", z, build)
    assert len(builds) == 4


def test_solver_same_point_constraint_fun_jac_cache_matches_uncached_counts(monkeypatch):
    service, request = request_v2()
    scipy_optimize = import_module("scipy.optimize")

    counts = []
    original = core._constraint_rows

    def counted(*args):
        counts.append(1)
        return original(*args)

    monkeypatch.setattr(core, "_constraint_rows", counted)

    def fake_minimize(fun, z, *, jac, constraints, **_kwargs):
        fun(z)
        jac(z)
        for _ in range(4):
            for c in constraints:
                c["fun"](z)
                c["jac"](z)
        return SimpleNamespace(x=z, success=True, status=0, message="diagnostic same-point")

    monkeypatch.setattr(scipy_optimize, "minimize", fake_minimize)
    cached = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert len(counts) == 1
    assert (
        cached.termination_reason == "no_improvement"
        and cached.improvement_status == "no_improvement"
    )
    assert (
        cached.forward_evaluations == 3
        and cached.probe_evaluations == 2
        and cached.fd_gradient_evaluations == 1
    )

    class NoCache:
        def get(self, _identity, _z, build):
            return build()

    monkeypatch.setattr(core, "_ConstraintRowsCache", NoCache)
    uncached = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert len(counts) > 2
    assert (
        uncached.baseline_analysis == cached.baseline_analysis
        and uncached.best_analysis == cached.best_analysis
    )
    assert uncached.best_guard == cached.best_guard
    for name in (
        "forward_evaluations",
        "jacobian_evaluations",
        "probe_evaluations",
        "evaluations",
        "cache_hits",
    ):
        assert getattr(uncached, name) == getattr(cached, name)
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(cached)
    )


def test_zero_budget_real_upper_can_improve_without_worsening_and_value_is_frozen():
    service, request = request_v2(delta=0.0)
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.usable_candidate and result.improved
    assert (
        result.best_analysis.nominal_conditions[0].actual
        <= result.baseline_analysis.nominal_conditions[0].actual
    )
    request = replace(
        request,
        objectives=(
            replace(request.objectives[0], mode="value", target_value=0.1, upper_bound=None),
        ),
    )
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.best_guard.guard_pass and result.improvement_status == "no_improvement"
    assert (
        result.best_analysis.nominal_conditions[0].actual
        == result.baseline_analysis.nominal_conditions[0].actual
    )


def test_resources_include_locked_baseline_readonly_cache_and_build_peak():
    _service, request = request_v2()
    resources = ideal.estimate_film_ideal_sensitivity_resources(request)
    m = resources["equality_constraints"] + resources["inequality_constraints"]
    minimum = 8 * m * (resources["free_variables"] + 1)
    assert resources["estimated_constraint_cache_bytes"] >= minimum
    assert resources["estimated_constraint_cache_build_bytes"] >= 2 * minimum
    assert resources["estimated_frozen_baseline_bytes"] >= 8 * resources["condition_points"]


def test_incumbent_F_worse_than_baseline_is_rejected_even_with_guard_pass():
    service, request = request_v2()
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    altered = replace(
        result,
        best_analysis=replace(
            result.best_analysis, objective_value=result.baseline_analysis.objective_value * 2
        ),
    )
    with pytest.raises(ValueError):
        ideal.validate_film_ideal_sensitivity_result_payload(
            ideal.film_ideal_sensitivity_result_to_payload(altered), replay=False
        )


def test_all_fixed_stays_analysis_only_and_zero_objective_is_transparent():
    service, request = request_v2()
    request = replace(
        request, variables=(replace(request.variables[0], lower_nm=80.0, upper_nm=80.0),)
    )
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.state == "failed" and result.termination_reason == "all_variables_fixed"
    assert result.improvement_status == "no_improvement"
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(result)
    )


def test_fd_probes_may_cross_guard_and_nominal_but_never_become_incumbents(monkeypatch):
    service, request = request_v2(delta=0.0, upper=0.001)
    scipy_optimize = import_module("scipy.optimize")

    probes = []
    original = core._Evaluator.scalar_objective

    def captured(self, design):
        value = original(self, design)
        # A separate test-only reference must not turn production scalar
        # probes into complete candidates or alter their cache/accounting.
        data = core._Evaluator(self.request).evaluate(design)
        assert value == data.analysis.objective_value
        probes.append((self.request, data))
        return value

    monkeypatch.setattr(core._Evaluator, "scalar_objective", captured)

    def once(fun, z, *, jac, **_kwargs):
        fun(z)
        jac(z)
        return SimpleNamespace(x=z, success=True, status=0, message="one complete gradient")

    monkeypatch.setattr(scipy_optimize, "minimize", once)
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert len(probes) == 2 and result.probe_evaluations == 2
    assert any(
        not core._guard(data, request, result.baseline_evidence).guard_pass
        for request, data in probes
    )
    assert all(not data.analysis.nominal_targets_met for _request, data in probes)
    assert (
        result.best_thickness_nm["low-film"] == 80.0
        and result.improvement_status == "no_improvement"
    )


def test_budget_stops_partial_gradient_without_delivering_it(monkeypatch):
    service, request = request_v2(two=True, max_evaluations=2)
    scipy_optimize = import_module("scipy.optimize")

    returned = []

    def once(fun, z, *, jac, **_kwargs):
        fun(z)
        returned.append(jac(z))
        raise AssertionError("half gradient must not escape")

    monkeypatch.setattr(scipy_optimize, "minimize", once)
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert not returned and result.fd_gradient_evaluations == 0
    assert result.forward_evaluations == result.jacobian_evaluations == 2
    assert result.termination_reason == "evaluation_limit" and result.usable_candidate
    assert result.best_guard.guard_pass
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(result)
    )


def test_replay_checks_actual_guard_without_reusing_response_compare_tolerance():
    service, request = request_v2(upper=0.001)
    design = deepcopy(request.design_snapshot)
    design["front_layers"][0]["thickness"]["value"] = 180.0
    request = replace(
        request,
        design_snapshot=design,
        variables=(replace(request.variables[0], lower_nm=130.0, upper_nm=230.0),),
    )
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    baseline = result.baseline_analysis.nominal_conditions[0].actual
    actual = result.best_analysis.nominal_conditions[0].actual
    # Forge a new narrower budget whose physical candidate is outside by 5e-14,
    # then self-consistently claim its response was 1e-13 lower (inside).
    budget = replace(request.budgets[0], delta=(actual - baseline - 5e-14) * 100.0)
    request = replace(result.frozen_request, budgets=(budget,))
    identity = core._request_identity(request)
    evidence = replace(result.baseline_evidence, request_identity=identity)
    evidence = replace(evidence, digest=core._evidence_digest(evidence))
    condition = replace(
        result.best_analysis.nominal_conditions[0],
        actual=actual - 1e-13,
        normalized_violation=actual - 1e-13 - 0.001,
        weighted_normalized_violation=actual - 1e-13 - 0.001,
    )
    analysis = replace(
        result.best_analysis,
        nominal_conditions=(condition,),
        nominal_max_normalized_error=condition.normalized_violation,
        nominal_weighted_rms_error=condition.normalized_violation,
    )
    evaluator = core._Evaluator(request, enforce_budget=False)
    physical = evaluator.evaluate(result.best_design)
    guard = core._guard(
        replace(physical, analysis=analysis, values=np.array([condition.actual])), request, evidence
    )
    assert guard.guard_pass and not core._guard(physical, request, evidence).guard_pass
    baseline_guard = core._guard(evaluator.evaluate(request.design_snapshot), request, evidence)
    altered = replace(
        result,
        frozen_request=request,
        cache_identity=identity,
        baseline_evidence=evidence,
        baseline_guard=baseline_guard,
        best_analysis=analysis,
        best_guard=guard,
    )
    payload = ideal.film_ideal_sensitivity_result_to_payload(altered)
    ideal.validate_film_ideal_sensitivity_result_payload(payload, replay=False)
    with pytest.raises(ValueError, match="guard"):
        ideal.validate_film_ideal_sensitivity_result_payload(payload, replay=True)


def test_replay_rejects_nextafter_F_forgery_of_no_improvement(monkeypatch):
    service, request = request_v2()
    scipy_optimize = import_module("scipy.optimize")

    def unchanged(_fun, z, **_kwargs):
        return SimpleNamespace(x=z, success=True, status=0, message="unchanged")

    monkeypatch.setattr(scipy_optimize, "minimize", unchanged)
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert not result.improved
    claimed = np.nextafter(result.baseline_analysis.objective_value, 0.0)
    altered = replace(
        result,
        termination_reason="slsqp_converged",
        best_analysis=replace(result.best_analysis, objective_value=claimed),
        best_solver_objective_value=claimed / result.solver_objective_scale,
    )
    assert altered.improved
    payload = ideal.film_ideal_sensitivity_result_to_payload(altered)
    ideal.validate_film_ideal_sensitivity_result_payload(payload, replay=False)
    with pytest.raises(ValueError, match="strict physical"):
        ideal.validate_film_ideal_sensitivity_result_payload(payload, replay=True)


def test_v2_result_parser_requires_explicit_policy():
    service, request = request_v2()
    result = ideal.analyze_film_ideal_sensitivity(service, request)
    payload = ideal.film_ideal_sensitivity_result_to_payload(result)
    payload.pop("policy")
    with pytest.raises(ValueError, match="explicit policy"):
        ideal.film_ideal_sensitivity_result_from_payload(payload)


def test_canonical_current_public_types_and_removed_compatibility_names():
    import importlib.util

    assert ideal.FilmIdealSensitivityRequest.__module__ == ideal.__name__
    assert ideal.FilmIdealSensitivityResult.__module__ == ideal.__name__
    for name in (
        "FilmIdealSensitivityV2Request", "FilmIdealSensitivityV2Result",
        "ideal_sensitivity_v2_request_from_optimization", "_as_v1", "_is_v2",
        "_v1_execute_film_ideal_sensitivity_request", "__getattr__",
        "_Evaluator", "_bounded_stencil",
    ):
        assert not hasattr(ideal, name)
    assert importlib.util.find_spec("coat_sim.core.film_ideal_sensitivity_v2") is None


@pytest.mark.parametrize("implementation", [ideal, core])
def test_kernel_identity_binds_public_and_private_current_implementation(monkeypatch, implementation):
    service, request = request_v2()
    frozen = ideal.freeze_film_ideal_sensitivity_request(service, request)
    original = core.inspect.getsource

    def changed_source(module):
        source = original(module)
        return source + "\n# changed current implementation\n" if module is implementation else source

    monkeypatch.setattr(core.inspect, "getsource", changed_source)
    assert core._kernel_identity() != frozen.kernel_identity
    with pytest.raises(ValueError, match="kernel identity"):
        ideal.film_ideal_sensitivity_request_to_payload(frozen)


def test_current_result_parser_rejects_schema1_instead_of_adapting():
    service, request = request_v2()
    result = ideal.analyze_film_ideal_sensitivity(service, request)
    payload = ideal.film_ideal_sensitivity_result_to_payload(result)
    payload["schema_version"] = 1
    with pytest.raises(ValueError, match="schema"):
        ideal.film_ideal_sensitivity_result_from_payload(payload)


@pytest.mark.parametrize("policy", ["strict_nominal", "bounded_baseline_degradation"])
def test_current_normalized_endpoint_is_exact_and_physically_replayable(monkeypatch, policy):
    service, request = request_v2(policy=policy)
    upper = 82.20098960421895
    request = replace(request, variables=(replace(request.variables[0],
        lower_nm=16.889770652853393, upper_nm=upper),))
    scipy_optimize: Any = import_module("scipy.optimize")

    def visit_upper(fun, x0, *, bounds, **_kwargs):
        point = np.asarray([bounds[0][1]])
        fun(point)
        return SimpleNamespace(x=point, success=True, status=0, message="endpoint")

    monkeypatch.setattr(scipy_optimize, "minimize", visit_upper)
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.state == "completed" and result.improved, result.message
    assert result.best_thickness_nm["low-film"] == upper
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(result))


def test_current_interior_initial_anchor_cannot_alias_representable_upper_endpoint(monkeypatch):
    service, request = request_v2()
    upper = float(np.nextafter(80.0, np.inf))
    request = replace(request, variables=(replace(request.variables[0],
        lower_nm=np.spacing(80.0) / 2, upper_nm=upper),))
    scipy_optimize: Any = import_module("scipy.optimize")
    seen = []
    original = core.simulate_with_thickness_jacobian

    def observed(design, *args, **kwargs):
        seen.append(design["front_layers"][0]["thickness"]["value"])
        return original(design, *args, **kwargs)

    def visit_upper(fun, x0, *, bounds, **_kwargs):
        assert 0.0 < x0[0] < 1.0
        point = np.asarray([bounds[0][1]])
        fun(point)
        return SimpleNamespace(x=point, success=True, status=0, message="endpoint")

    monkeypatch.setattr(scipy_optimize, "minimize", visit_upper)
    monkeypatch.setattr(core, "simulate_with_thickness_jacobian", observed)
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.state == "completed", result.message
    assert result.evaluations == 2 and seen == [80.0, upper]
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(result))


def test_current_real_slsqp_never_archives_one_ulp_above_original_physical_bound():
    service, request = request_v2()
    upper = 82.20098960421895
    request = replace(request, variables=(replace(request.variables[0],
        lower_nm=16.889770652853393, upper_nm=upper),))
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.state == "completed" and result.improved, result.message
    assert result.best_thickness_nm["low-film"] <= upper
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(result))
