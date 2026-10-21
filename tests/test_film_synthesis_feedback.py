"""Fixed local feedback preserves original score identity and audit boundaries."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import math
from typing import Any, cast

import numpy as np
import pytest

from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
from coat_sim.core.film_optimization import FilmConditionError, FilmOptimizationObjective
from coat_sim.core.film_synthesis_feedback import (
    SparseFeedbackRegistry, augment_feedback_guide, feedback_candidate_key,
    restore_feedback_base,
)
from coat_sim.core.film_synthesis_refinement import interior_loss_gradient

pytestmark = pytest.mark.algorithm


def request_fixture():
    objectives = (
        FilmOptimizationObjective(500., 600., 3, angles_deg=(0., 45.), polarization="s",
            mode="range", upper_bound=.1, weight=2., normalization_scale=.02),
        FilmOptimizationObjective(500., 600., 3, polarization="p", metric="T",
            mode="range", lower_bound=.9, upper_bound=1., normalization_scale=.1),
    )
    return FilmSynthesisRequest({}, objectives, (), acceptable_normalized_error=.01,
        strict_lower_bound_objective_indices=(1,))


def error_for(request, index, wave, actual, angle=0.):
    objective = request.objectives[index]
    violation = max(0., 0. if objective.lower_bound is None else objective.lower_bound - actual,
        0. if objective.upper_bound is None else actual - objective.upper_bound)
    normalized = violation / objective.normalization_scale
    return FilmConditionError(index, wave, angle, objective.polarization, objective.metric,
        actual, objective.target_value, objective.lower_bound, objective.upper_bound,
        normalized, normalized * math.sqrt(objective.weight))


def base_fixture(request):
    errors = tuple(error_for(request, i, wave, .05 if i == 0 else .95, angle)
        for i, objective in enumerate(request.objectives) for angle in objective.angles_deg
        for wave in (500., 550., 600.))
    responses = tuple(dict(objective_index=i, angle_deg=angle,
        wavelength_nm=[500., 550., 600.], actual_values=[.05 if i == 0 else .95] * 3,
        jacobian_values_by_layer_id={"layer": [0., 0., 0.]})
        for i, objective in enumerate(request.objectives) for angle in objective.angles_deg)
    return dict(design={"front_layers": [{"id": "layer"}]},
        score=dict(errors=errors, max_error=0., weighted_rms_error=0.), responses=responses)


def test_registry_excludes_training_grid_and_repeated_points_before_selecting_new_worst():
    request = request_fixture()
    registry = SparseFeedbackRegistry(request)
    # Existing training conditions and one-ULP serialization drift add no work.
    assert registry.record_audit({"errors": [error_for(request, 0, 500., .9),
        error_for(request, 0, math.nextafter(550., math.inf), .9),
        error_for(request, 1, 600., .9)]}) == 0
    assert registry.snapshot().revision == 0
    errors = [error_for(request, 0, 525., .2), error_for(request, 0, 575., .15),
              error_for(request, 0, 525., .3, 45.)]
    assert registry.record_audit({"errors": errors}) == 2
    first = registry.snapshot()
    assert {(p.wavelength_nm, p.angle_deg) for p in first.points} == {(525., 0.), (525., 45.)}
    assert registry.record_audit({"errors": errors}) == 1  # Next unseen failure, not old maximum.
    assert registry.record_audit({"errors": errors}) == 0
    assert len(first.points) == 2 and first.revision == 1
    assert len(registry.snapshot().points) == 3 and registry.snapshot().revision == 2
    with pytest.raises(FrozenInstanceError):
        cast(Any, first).revision = 99


def test_strict_equality_outside_grid_is_a_failure_and_narrow_near_grid_peak_is_retained():
    request = request_fixture()
    registry = SparseFeedbackRegistry(request)
    assert registry.record_audit({"errors": [error_for(request, 1, 525., .9),
        error_for(request, 0, 550. + 1e-8, .2)]}) == 2
    points = registry.snapshot().points
    assert points[0].wavelength_nm == 550. + 1e-8
    assert points[1].objective_index == 1 and points[1].polarization == "p"


@pytest.mark.parametrize("mutation", ["metric", "polarization", "angle", "index", "score"])
def test_registry_condition_identity_is_checked_atomically(mutation):
    request = request_fixture()
    valid = error_for(request, 0, 525., .2)
    updates: dict[str, dict[str, Any]] = {"metric": {"metric": "T"}, "polarization": {"polarization": "p"},
        "angle": {"angle_deg": 15.}, "index": {"objective_index": 12},
        "score": {"normalized_violation": 0.}}
    registry = SparseFeedbackRegistry(request)
    with pytest.raises(ValueError):
        registry.record_audit({"errors": [valid, replace(valid, **updates[mutation])]})
    assert registry.snapshot().revision == 0 and registry.snapshot().points == ()


def populated_snapshot(request):
    registry = SparseFeedbackRegistry(request)
    registry.record_audit({"errors": [error_for(request, 1, 525., .9),
                                      error_for(request, 0, 575., .2, 45.)]})
    return registry.snapshot()


def point_evaluation(base, objectives, delta=0.) -> dict[str, Any]:
    temporary = replace(request_fixture(), objectives=objectives)
    actuals = [(.2 if objective.metric == "R" else .9) + .03 * delta for objective in objectives]
    return dict(design=deepcopy(base["design"]),
        score=dict(errors=tuple(error_for(temporary, i, objective.wavelength_min_nm, actuals[i],
            objective.angles_deg[0]) for i, objective in enumerate(objectives))),
        responses=tuple(dict(objective_index=i, angle_deg=objective.angles_deg[0],
            wavelength_nm=[objective.wavelength_min_nm], actual_values=[actuals[i]],
            jacobian_values_by_layer_id={"layer": [.03]}) for i, objective in enumerate(objectives)))


def test_guide_restores_exact_base_and_remaps_singletons_without_changing_frozen_objectives():
    request = request_fixture()
    snapshot, base = populated_snapshot(request), base_fixture(request)
    original = deepcopy(base)
    calls = []
    def evaluate(objectives):
        calls.append(objectives)
        return point_evaluation(base, objectives)
    guide = augment_feedback_guide(request, base, snapshot, evaluate_points=evaluate)
    assert len(calls) == 1 and len(calls[0]) == 2
    for point, objective in zip(snapshot.points, calls[0], strict=True):
        original_objective = request.objectives[point.objective_index]
        assert objective == replace(original_objective, wavelength_min_nm=point.wavelength_nm,
            wavelength_max_nm=point.wavelength_nm, wavelength_samples=1, angles_deg=(point.angle_deg,))
    assert base == original
    assert guide["score"]["errors"] == base["score"]["errors"]
    assert guide["score"]["weighted_rms_error"] == base["score"]["weighted_rms_error"]
    assert guide["score"]["max_error"] == pytest.approx(5.)
    assert [row["objective_index"] for row in guide["responses"][-2:]] == [0, 1]
    assert restore_feedback_base(guide) is base
    assert restore_feedback_base(base) is base
    assert feedback_candidate_key(guide, 1.)[0] == 1


def test_feedback_analytic_guide_derivative_matches_finite_difference():
    request = request_fixture()
    snapshot, base = populated_snapshot(request), base_fixture(request)
    def guide_at(delta):
        return augment_feedback_guide(request, base, snapshot,
            evaluate_points=lambda objectives: point_evaluation(base, objectives, delta))
    value = guide_at(0.)
    _, gradient = interior_loss_gradient(request, value, ("layer",))
    step = 1e-6
    numeric = (interior_loss_gradient(request, guide_at(step), ("layer",))[0]
               - interior_loss_gradient(request, guide_at(-step), ("layer",))[0]) / (2 * step)
    assert gradient[0] == pytest.approx(numeric, rel=1e-7)
    # Strict equality can have zero ordinary violation yet loses to a strict pass.
    equal = deepcopy(value)
    equal["score"]["max_error"] = 0.
    passed = deepcopy(equal)
    passed["_feedback_strict_failed"] = False
    assert feedback_candidate_key(passed, 2.) < feedback_candidate_key(equal, 0.)


def test_callback_stop_propagates_and_original_base_remains_unchanged():
    request = request_fixture()
    base = base_fixture(request)
    original = deepcopy(base)
    class Stopped(Exception):
        pass
    def stopped(_objectives):
        raise Stopped("deadline")
    with pytest.raises(Stopped, match="deadline"):
        augment_feedback_guide(request, base, populated_snapshot(request), evaluate_points=stopped)
    assert base == original


@pytest.mark.parametrize("mutation", ["request", "missing_responses", "wrong_design", "wrong_response"])
def test_guide_rejects_cross_condition_or_incomplete_values(mutation):
    request = request_fixture()
    base, snapshot = base_fixture(request), populated_snapshot(request)
    if mutation == "request":
        request = replace(request, objectives=tuple(reversed(request.objectives)))
    if mutation == "missing_responses":
        base.pop("responses")
    def evaluate(objectives):
        extra = point_evaluation(base, objectives)
        if mutation == "wrong_design":
            extra["design"] = {}
        if mutation == "wrong_response":
            extra["responses"][0]["angle_deg"] = 123.
        return extra
    with pytest.raises(ValueError):
        augment_feedback_guide(request, base, snapshot, evaluate_points=evaluate)


def test_empty_snapshot_never_calls_physics_and_preserves_base_strict_failure():
    request = request_fixture()
    base = base_fixture(request)
    base["score"]["errors"] = (*base["score"]["errors"], error_for(request, 1, 500., .9))
    def unexpected(_objectives):
        raise AssertionError("No sparse points exist")
    guide = augment_feedback_guide(request, base, SparseFeedbackRegistry(request).snapshot(),
        evaluate_points=unexpected)
    assert feedback_candidate_key(guide, 0.) == (1, 0., 0.)
    assert restore_feedback_base(guide) is base


def test_real_physical_feedback_keeps_grid_score_and_jacobian_identity():
    from coat_sim.core import film_auto_synthesis as synthesis
    from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
    from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate
    from tests.test_film_auto_synthesis import setup_problem
    service, request = setup_problem(upper=0., low=520., high=580., samples=3)
    request = synthesis.freeze_film_synthesis_request(service, request)
    design = deepcopy(dict(request.environment))
    design["front_layers"] = [dict(id="feedback-real", material_id="low", optical_set_id="nk",
        thickness=dict(value=40., unit="nm"), note="")]
    def physical(objectives):
        return evaluate_frozen_structure_candidate(synthesis._proxy(replace(request, objectives=objectives)),
            design, variable_ids=("feedback-real",), with_jacobian=True, derived_validation_grid=False)
    base = physical(request.objectives)
    audited = audit_frozen_synthesis_candidate(request, design)
    registry = SparseFeedbackRegistry(request)
    assert registry.record_audit(audited["score"]) > 0
    guide = augment_feedback_guide(request, base, registry.snapshot(), evaluate_points=physical)
    assert len(base["score"]["errors"]) == 3
    assert guide["score"]["errors"] == base["score"]["errors"]
    loss, gradient = interior_loss_gradient(request, guide, ("feedback-real",))
    assert math.isfinite(loss) and np.all(np.isfinite(gradient))
    assert restore_feedback_base(guide) is base
