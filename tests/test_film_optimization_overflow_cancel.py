"""Uncapped baseline search stays finite and cancellable after step overflow."""
from __future__ import annotations

import math

from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import (
    FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable,
    optimize_film_design,
)
from tests.test_film_reduction import Service, layer, request


def _overflow_source() -> FilmOptimizationRequest:
    goal = FilmOptimizationObjective(550, 550, 1,
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=0.1))
    design = request([layer("large", 1.7e308)], objective=goal).base_request.design_snapshot
    return FilmOptimizationRequest(design, 1,
        (FilmThicknessVariable("front", "large", 0., None),), (goal,),
        initial_step_fraction=1., min_step_fraction=1e-4)


def _response(design, *_):
    thickness = design["front_layers"][0]["thickness"]["value"]
    assert math.isfinite(thickness)
    return 0.2 + 0.6 * (thickness / 1.7e308)


def test_unlimited_adaptive_overflow_can_shrink_and_stop_naturally():
    service = Service(_response)
    result = optimize_film_design(service, _overflow_source(), baseline_limits=(None, None))
    assert result.state == "completed"
    assert result.termination_reason == "converged"
    assert result.best_thickness_nm == {"large": 0.}
    assert not result.targets_met
    assert result.validation_complete
    assert 2 < result.evaluations < 100
    assert 1 < result.iterations < 100
    assert result.evaluations + result.validation_evaluations == len(service.calls)


def test_late_cancellation_is_checked_without_another_complete_evaluation():
    service = Service(_response)

    class CancelAfterImprovedCandidate:
        checks_after_improvement = 0

        def is_set(self) -> bool:
            if len(service.calls) >= 2:
                self.checks_after_improvement += 1
            # consider() first observes False after committing the improved
            # point. The next loop boundary must refresh it without optics.
            return self.checks_after_improvement >= 2

    result = optimize_film_design(service, _overflow_source(),
        baseline_limits=(None, None), cancel_event=CancelAfterImprovedCandidate())
    assert result.state == result.termination_reason == "cancelled"
    assert result.best_thickness_nm == {"large": 0.}
    assert result.evaluations == len(service.calls) == 2
    assert result.iterations == 1
    assert not result.validation_complete and result.validation_evaluations == 0
