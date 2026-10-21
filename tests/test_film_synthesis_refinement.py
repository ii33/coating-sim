from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_optimization_structure as physical
from coat_sim.core.film_auto_synthesis import (
    FilmSynthesisRequest, freeze_film_synthesis_request,
)
from coat_sim.core.film_design import new_design
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_synthesis_refinement import (
    interior_bounds, interior_loss_gradient, refine_synthesis_candidate,
)
from tests.test_film_auto_synthesis import setup_problem


def test_analytic_interior_gradient_matches_finite_difference_and_keeps_physical_endpoint():
    objective = FilmOptimizationObjective(
        500., 600., 2, mode="range", lower_bound=.8, upper_bound=.9,
        weight=1.7, normalization_scale=2.3,
    )
    request = FilmSynthesisRequest({}, (objective,), (), acceptable_normalized_error=.01)
    assert interior_bounds(objective, .01) == pytest.approx((.81, .89))

    actual = np.array([.7, .95])
    jacobian = np.array([.2, -.1])

    def loss_at(delta):
        value = {"responses": [{"objective_index": 0,
            "actual_values": (actual + delta * jacobian).tolist(),
            "jacobian_values_by_layer_id": {"layer-a": jacobian.tolist()}}]}
        return interior_loss_gradient(request, value, ("layer-a",))[0]

    value = {"responses": [{"objective_index": 0,
        "actual_values": actual.tolist(),
        "jacobian_values_by_layer_id": {"layer-a": jacobian.tolist()}}]}
    loss, gradient = interior_loss_gradient(request, value, ("layer-a",))
    step = 1e-6
    finite_difference = (loss_at(step) - loss_at(-step)) / (2 * step)
    assert gradient[0] == pytest.approx(finite_difference, rel=1e-7, abs=1e-9)

    high_r = replace(objective, lower_bound=.99, upper_bound=1.)
    assert interior_bounds(high_r, .001) == pytest.approx((.991, 1.))
    high_request = replace(request, objectives=(high_r,), acceptable_normalized_error=.001)
    high_value = {"responses": [{"objective_index": 0, "actual_values": [.9999],
        "jacobian_values_by_layer_id": {"layer-a": [5.]}}]}
    high_loss, high_gradient = interior_loss_gradient(high_request, high_value, ("layer-a",))
    assert high_loss == 0.
    assert high_gradient[0] == 0.
    full_physical_range = replace(objective, lower_bound=0., upper_bound=1.)
    assert interior_bounds(full_physical_range, .01) == (0., 1.)


def _physical_refinement_fixture(*, total_cap=None):
    service, base_request = setup_problem(low=550., high=550., samples=1, materials=("low", "high"))
    objective = FilmOptimizationObjective(
        550., 550., 1, angles_deg=(0.,), polarization="unpolarized",
        metric="R", mode="range", lower_bound=.9945, upper_bound=1.,
    )
    environment = new_design(design_id="known-high-r-refinement")
    environment["substrate"].update(material_id="glass", optical_set_id="nk")
    request = FilmSynthesisRequest(
        environment, (objective,), base_request.candidate_materials,
        minimum_nonzero_thickness_nm=10., max_layer_thickness_nm=120.,
        max_layers=12, max_total_thickness_nm=total_cap,
        acceptable_normalized_error=.001, validation_samples_multiplier=2,
    )
    frozen = freeze_film_synthesis_request(service, request)
    high_q = 550. / (4. * 2.2)
    low_q = 550. / (4. * np.sqrt(1.5))
    design = dict(environment)
    design["front_layers"] = []
    for index in range(6):
        for material, thickness in (("high", high_q * (1.25 if index == 0 else 1.0)),
                                    ("low", low_q)):
            design["front_layers"].append({
                "id": f"hr-{index}-{material}", "material_id": material,
                "optical_set_id": "nk", "thickness": {"value": thickness, "unit": "nm"},
                "note": "synthetic physical refinement fixture",
            })
    if total_cap is not None and total_cap < sum(row["thickness"]["value"] for row in design["front_layers"]):
        raise ValueError("fixture cap must include its initial stack")
    proxy = synthesis._proxy(frozen)
    layer_ids = tuple(row["id"] for row in design["front_layers"])
    quarter = {("high", "nk"): high_q, ("low", "nk"): low_q}

    def evaluate(candidate, *, jacobian=True):
        return physical.evaluate_frozen_structure_candidate(
            proxy, candidate, variable_ids=layer_ids if jacobian else (), with_jacobian=jacobian)

    initial = evaluate(design)
    return frozen, design, layer_ids, quarter, evaluate, initial


def test_physical_high_reflectance_refinement_passes_original_strict_lower_bound_and_caps():
    high_q = 550. / (4. * 2.2)
    low_q = 550. / (4. * np.sqrt(1.5))
    total_cap = sum([high_q * 1.25, low_q] + [value for _ in range(5)
                                               for value in (high_q, low_q)]) + 100.
    request, design, layer_ids, quarter, evaluate, initial = _physical_refinement_fixture(
        total_cap=total_cap)
    assert initial["score"]["max_error"] > 0.
    assert min(initial["responses"][0]["actual_values"]) < .9945
    archived = []
    progress = []
    best, diagnostics = refine_synthesis_candidate(
        request, initial,
        evaluate=evaluate, archive=lambda value, *, audit_eligible: archived.append((value, audit_eligible)),
        quarter_nm=quarter, progress=lambda stage, value, detail: progress.append((stage, value, detail)),
        max_iterations=64,
    )
    assert len(best["design"]["front_layers"]) == len(layer_ids) == 12
    assert best["score"]["max_error"] <= .001
    assert min(value for row in best["responses"] for value in row["actual_values"]) >= .9945
    for candidate, audit_eligible in archived:
        assert audit_eligible is False
        layers = candidate["design"]["front_layers"]
        thicknesses = [row["thickness"]["value"] for row in layers]
        assert len(layers) <= request.max_layers
        assert all(request.minimum_nonzero_thickness_nm <= d <= request.max_layer_thickness_nm
                   for d in thicknesses)
        assert sum(thicknesses) <= request.max_total_thickness_nm + 1e-7
    assert archived and all(audit_eligible is False for _, audit_eligible in archived)
    assert progress and diagnostics["physical_candidates"] >= 1
    assert diagnostics["interior_bounds"] == [(.99505, 1.)]


@pytest.mark.parametrize("failure_kind", ["cancel", "budget"])
def test_refinement_propagates_parent_cancel_and_budget_without_audit(failure_kind):
    request, _, _, quarter, _, initial = _physical_refinement_fixture()
    class ParentBoundary(Exception):
        pass

    calls = []
    audits = []

    def evaluate(_design, *, jacobian=True):
        calls.append(jacobian)
        raise ParentBoundary(failure_kind)

    def cancel_check():
        if failure_kind == "cancel":
            raise ParentBoundary("cancel")

    with pytest.raises(ParentBoundary, match=failure_kind):
        refine_synthesis_candidate(request, initial, evaluate=evaluate,
            archive=lambda _value, *, audit_eligible: audits.append(audit_eligible),
            quarter_nm=quarter, cancel_check=cancel_check)
    assert audits == []
    assert calls == ([] if failure_kind == "cancel" else [True])


@pytest.mark.parametrize("cached_responses", [True, False])
def test_retained_initial_has_finite_or_explicitly_unavailable_guide_without_extra_physics(monkeypatch, cached_responses):
    from copy import deepcopy
    import json
    from types import SimpleNamespace
    import scipy.optimize

    request, _, _, quarter, _, initial = _physical_refinement_fixture()
    initial = deepcopy(initial)
    initial["score"]["max_error"] = .1
    if cached_responses:
        # Structural forward candidates may have values without a Jacobian.
        for response in initial["responses"]:
            response.pop("jacobian_values_by_layer_id", None)
    else:
        initial.pop("responses")  # Production workers intentionally omit these.
    evaluated = []
    archived = []
    def evaluate(design, *, jacobian):
        assert jacobian
        evaluated.append(deepcopy(design))
        value = {"design": deepcopy(design), "score": {"max_error": np.nextafter(.1, np.inf)},
                 "responses": [{"objective_index": 0, "actual_values": [.99],
                   "jacobian_values_by_layer_id": {row["id"]: [0.] for row in design["front_layers"]}}]}
        return value
    def solver(objective, point, **_options):
        objective(point)  # Initial parent evaluation is already cached.
        trial = np.array(point, copy=True)
        trial[0] += .01
        objective(trial)
        return SimpleNamespace(message="fixed regression trial schedule", nit=1)
    monkeypatch.setattr(scipy.optimize, "minimize", solver)
    best, diagnostics = refine_synthesis_candidate(request, initial, evaluate=evaluate,
        archive=lambda value, *, audit_eligible: archived.append((value, audit_eligible)), quarter_nm=quarter)
    assert best is initial  # Every re-evaluated score is one ULP worse.
    assert len(evaluated) == diagnostics["physical_candidates"] == 2
    assert len(archived) == 2 and all(flag is False for _, flag in archived)
    if cached_responses:
        from coat_sim.core.film_synthesis_refinement import interior_loss
        assert diagnostics["interior_loss"] == interior_loss(request, initial)
        assert np.isfinite(diagnostics["interior_loss"])
        assert diagnostics["interior_loss_source"] == "initial_cached_responses"
    else:
        assert diagnostics["interior_loss"] is None
        assert "unavailable_without_additional_physical_evaluation" in diagnostics["interior_loss_source"]
    json.dumps({"synthesis_performance": {"search": {"local_refinement": [diagnostics]}}}, allow_nan=False)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_nonfinite_cached_initial_response_is_rejected_without_physical_call(bad):
    from copy import deepcopy
    request, _, _, quarter, _, initial = _physical_refinement_fixture()
    initial = deepcopy(initial)
    initial["responses"][0]["actual_values"][0] = bad
    def unexpected(*_args, **_kwargs):
        pytest.fail("invalid cached physical response must fail before any extra evaluation")
    with pytest.raises(ValueError, match="nonfinite interior refinement response"):
        refine_synthesis_candidate(request, initial, evaluate=unexpected, archive=unexpected, quarter_nm=quarter)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_nonfinite_trial_jacobian_is_rejected_not_sanitized(bad):
    from copy import deepcopy
    request, _, layer_ids, quarter, evaluate, initial = _physical_refinement_fixture()
    def invalid_jacobian(design, *, jacobian):
        value = deepcopy(evaluate(design, jacobian=jacobian))
        value["responses"][0]["jacobian_values_by_layer_id"][layer_ids[0]][0] = bad
        return value
    with pytest.raises(ValueError, match="nonfinite interior refinement loss or derivative"):
        refine_synthesis_candidate(request, initial, evaluate=invalid_jacobian,
            archive=lambda *_args, **_kwargs: pytest.fail("invalid derivative must not be archived"), quarter_nm=quarter)


@pytest.mark.parametrize("method", ["L-BFGS-B", "SLSQP"])
def test_zero_tolerance_roundoff_boundary_real_solvers_enter_original_range(method):
    from copy import deepcopy
    import json

    # Analytic boundary counterexample; both real SciPy solvers previously
    # stopped at this point because the eight-ULP guide gradient was tiny.
    lower = .99
    original_actual = .9899999999994828
    objective = FilmOptimizationObjective(550., 550., 1, mode="range", lower_bound=lower, upper_bound=1.)
    request = FilmSynthesisRequest({}, (objective,), (), acceptable_normalized_error=0.,
        strict_lower_bound_objective_indices=(0,), max_total_thickness_nm=160. if method == "SLSQP" else None)
    original_objectives = deepcopy(request.objectives)
    design = {"front_layers": [{"id": "analytic-layer", "material_id": "analytic", "optical_set_id": "nk",
                               "thickness": {"value": 100., "unit": "nm"}}]}
    calls = []
    archived = []
    def response(candidate):
        thickness = candidate["front_layers"][0]["thickness"]["value"]
        actual = original_actual + .001 * (thickness - 100.)
        return {"design": deepcopy(candidate), "score": {"max_error": max(lower - actual, 0.)},
                "responses": [{"objective_index": 0, "actual_values": [actual],
                               "jacobian_values_by_layer_id": {"analytic-layer": [.001]}}]}
    def evaluate(candidate, *, jacobian):
        assert jacobian
        calls.append(deepcopy(candidate))
        return response(candidate)
    initial = response(design)
    assert initial["responses"][0]["actual_values"][0] < lower
    best, diagnostics = refine_synthesis_candidate(request, initial, evaluate=evaluate,
        archive=lambda value, *, audit_eligible: archived.append((value, audit_eligible)),
        quarter_nm={("analytic", "nk"): 100.})
    assert diagnostics["method"] == method
    assert best["responses"][0]["actual_values"][0] > lower
    assert best["score"]["max_error"] == 0.
    assert request.objectives == original_objectives
    assert request.acceptable_normalized_error == 0.
    assert diagnostics["physical_candidates"] == len(calls) == len(archived)
    assert 1 < len(calls) < 16
    assert all(audit_eligible is False for _, audit_eligible in archived)
    assert all(50. <= candidate["front_layers"][0]["thickness"]["value"] <= 150. for candidate in calls)
    json.dumps(diagnostics, allow_nan=False)


def test_numeric_guide_floor_preserves_default_physical_endpoints_value_mode_and_width_cap():
    import json
    from dataclasses import asdict

    high_r = FilmOptimizationObjective(550., 550., 1, mode="range", lower_bound=.99, upper_bound=1.)
    before = asdict(high_r)
    assert interior_bounds(high_r, 0.) == pytest.approx((.99001, 1.), rel=0., abs=2e-16)
    assert interior_bounds(high_r, .001) == pytest.approx((.991, 1.), rel=0., abs=2e-16)
    ar = replace(high_r, lower_bound=None, upper_bound=.02)
    assert interior_bounds(ar, 0.) == pytest.approx((None, .01998))
    assert interior_bounds(replace(high_r, lower_bound=0., upper_bound=1.), 0.) == (0., 1.)
    value_target = replace(high_r, mode="value", target_value=.99, lower_bound=None, upper_bound=None)
    assert interior_bounds(value_target, 0.) == (None, None)
    fixed_range = replace(high_r, lower_bound=.5, upper_bound=.5)
    assert interior_bounds(fixed_range, 0.) == (.5, .5)
    narrow = replace(high_r, lower_bound=.5, upper_bound=.500001)
    low_guide, high_guide = interior_bounds(narrow, 1.)
    width = narrow.upper_bound - narrow.lower_bound
    assert narrow.lower_bound < low_guide < high_guide < narrow.upper_bound
    assert low_guide - narrow.lower_bound == pytest.approx(width * .1, abs=1e-16)
    assert narrow.upper_bound - high_guide == pytest.approx(width * .1, abs=1e-16)
    assert asdict(high_r) == before
    json.dumps({"bounds": interior_bounds(high_r, 0.)}, allow_nan=False)
