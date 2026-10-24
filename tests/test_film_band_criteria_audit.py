"""Raw spectral evidence and enabled-only nominal/audit/display contracts."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from coat_sim.core.film_candidate_display import CandidateDisplayObserver
from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_nominal_acceptance import (
    complete_original_score, nominal_targets_pass, solver_acceptable,
)
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint, reduce_objective_responses
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_scoring import reduction_score
from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate, objective_constraint_status

pytestmark = pytest.mark.algorithm


def _objective(*, mean=True, pointwise=False, angles=(0.,), strict=False):
    return FilmOptimizationObjective(500., 600., 3, angles_deg=angles,
        mean=FilmObjectiveConstraint(enabled=mean, lower_bound=.5, strict_lower=strict),
        pointwise=FilmObjectiveConstraint(enabled=pointwise, lower_bound=.5))


def _score(objective, values, *, tolerance=0.):
    frames = tuple(dict(objective_index=0, angle_deg=angle, wavelength_nm=(500., 550., 600.),
                        actual_values=tuple(actual))
                   for angle, actual in zip(objective.angles_deg, values, strict=True))
    reduced = reduce_objective_responses((objective,), frames, tolerance=tolerance)
    request = SimpleNamespace(objectives=(objective,), acceptable_normalized_error=tolerance,
                              validation_samples_multiplier=2)
    return request, reduction_score(reduced, tolerance)


@pytest.mark.parametrize("pointwise,expected", [(False, True), (True, False)])
def test_enabled_criteria_are_anded_without_a_disabled_point_gate(pointwise, expected):
    request, score = _score(_objective(pointwise=pointwise), ((0., 1., 0.),))
    assert complete_original_score(request, score)
    assert nominal_targets_pass(request, score) is expected
    assert solver_acceptable(request, score) is expected
    means = [e for e in score["errors"] if e.reduction == "mean"]
    assert len(means) == 1 and means[0].actual == .5 and means[0].wavelength_nm is None
    assert score["weighted_rms_error"] == 0. if not pointwise else score["weighted_rms_error"] > 0.


def test_angles_are_checked_separately_and_report_the_failing_angle():
    objective = replace(_objective(angles=(0., 45.)), mean=FilmObjectiveConstraint(lower_bound=.7))
    request, score = _score(objective, ((.9,) * 3, (.5,) * 3))
    assert complete_original_score(request, score)
    assert not nominal_targets_pass(request, score)
    status = objective_constraint_status(request.objectives, score["errors"], 0.)[0]
    assert status["mean"]["worst_angle_deg"] == 45.
    assert [a["targets_met"] for a in status["mean"]["angles"]] == [True, False]
    assert status["pointwise"] == {"enabled": False}


def test_one_mean_row_cannot_stand_in_for_missing_raw_grid_or_a_forged_mean():
    request, score = _score(_objective(), ((0., 1., 0.),))
    assert complete_original_score(request, score)
    assert not complete_original_score(request, {k: v for k, v in score.items() if k != "coverage"})
    coverage = score["coverage"][0]
    sparse = replace(coverage, wavelength_nm=(500., 600.), actual_values=(.5, .5))
    assert not complete_original_score(request, dict(score, coverage=(sparse,)))
    row = replace(score["errors"][0], actual=.50000000000001)
    assert not complete_original_score(request, dict(score, errors=(row,)))
    assert not complete_original_score(request, dict(score, errors=(replace(row, wavelength_nm=550.),)))


def test_strict_boundary_has_zero_hinge_but_fails_both_acceptance_flags():
    request, score = _score(_objective(strict=True), ((.5,) * 3,), tolerance=.1)
    assert score["max_error"] == 0.
    assert complete_original_score(request, score)
    assert not nominal_targets_pass(request, score)
    assert not solver_acceptable(request, score)
    request, score = _score(_objective(), ((.49,) * 3,), tolerance=.02)
    assert not nominal_targets_pass(request, score)
    assert solver_acceptable(request, score)


@pytest.mark.parametrize("metadata_only", [False, True])
def test_display_scores_enabled_rows_and_previews_the_complete_raw_spectrum(metadata_only):
    objective = _objective()
    _request, score = _score(objective, ((0., 1., 0.),))
    packets: list[dict[str, Any]] = []
    observer = CandidateDisplayObserver(context_id="band-only", objectives=(objective,),
        control_provider=lambda: dict(enabled=True), publish_callback=packets.append)
    kwargs = dict(weighted_rms_error=score["weighted_rms_error"], max_normalized_error=score["max_error"],
                  condition_rows=None if metadata_only else score["errors"],
                  nominal_pass=score["nominal_pass"], condition_count=score["condition_count"],
                  physical_thickness_nm=(), is_best=True)
    observer.record(new_design(), **kwargs)
    assert not packets  # One mean row alone never proves raw coverage.
    observer.record(new_design(), raw_coverage=score["coverage"], **kwargs)
    assert len(packets) == 1
    assert packets[0]["current"]["nominal_pass"]
    assert packets[0]["current"]["condition_count"] == 1
    assert packets[0]["current"]["spectrum"]["actual"] == [0., 1., 0.]
    assert packets[0]["top10"][0]["weighted_rms_error"] == 0.


class _Materials:
    def __init__(self, records):
        self.records = records

    def _records(self):
        return deepcopy(self.records)

    def get(self, identifier):
        return deepcopy(next(row for row in self.records if row["id"] == identifier))


class _Service:
    validate = staticmethod(validate_design)
    freeze_design = FilmDesignService.freeze_design

    def __init__(self):
        self.materials = _Materials([dict(id=name, name=name, design_ready=True, optical_sets=[dict(
            id="nk", name=name, kind="tabulated", notes="synthetic band audit fixture",
            source="synthetic numerical fixture; not external material evidence", wavelength_unit="nm",
            wavelength_min=450., wavelength_max=700., wavelengths=[450., 550., 700.],
            n=[index] * 3, k=[0.] * 3)]) for name, index in (("glass", 1.5), ("low", 1.3), ("high", 2.2))])


def _frozen_request(objective, *, glass_index=1.5):
    from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest, freeze_film_synthesis_request
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference

    service = _Service()
    service.materials.records[0]["optical_sets"][0]["n"] = [glass_index] * 3
    environment = new_design()
    environment["substrate"].update(material_id="glass", optical_set_id="nk")
    request = FilmSynthesisRequest(environment, (objective,),
        tuple(FilmNeedleMaterialReference(name, "nk") for name in ("low", "high")),
        max_cycles=1, max_evaluations=10, validation_samples_multiplier=2)
    return freeze_film_synthesis_request(service, request)


@pytest.mark.parametrize("pointwise", [False, True])
def test_audit_auxiliary_points_never_reweight_mean_or_redefine_the_original_band(monkeypatch, pointwise):
    from coat_sim.core import film_synthesis_audit as audit_module

    objective = replace(_objective(pointwise=pointwise),
        mean=FilmObjectiveConstraint(lower_bound=.7),
        pointwise=FilmObjectiveConstraint(enabled=pointwise, lower_bound=.7))
    request = _frozen_request(objective)

    def synthetic_evaluate(_owner, design, *, objectives, **_kwargs):
        # Dense nodes have .8, shifted nodes have 0: adding shifted nodes to
        # integration would change the declared .8 mean into a failing mean.
        responses = []
        for index, local in enumerate(objectives):
            waves = np.linspace(local.wavelength_min_nm, local.wavelength_max_nm, local.wavelength_samples)
            actual = .8 * np.cos(np.pi * (waves - 500.) / 25.) ** 2
            for angle in local.angles_deg:
                responses.append(dict(objective_index=index, angle_deg=angle,
                                      wavelength_nm=waves, actual_values=actual))
        reduced = reduce_objective_responses(objectives, responses,
                                             tolerance=request.acceptable_normalized_error)
        return dict(design=deepcopy(design), responses=tuple(responses),
                    score=reduction_score(reduced, request.acceptable_normalized_error))

    monkeypatch.setattr(audit_module._PreparedStructureEvaluator, "evaluate", synthetic_evaluate)
    value = audit_frozen_synthesis_candidate(request, request.environment)
    score = value["score"]
    assert complete_original_score(request, score, independent_audit=True)
    assert nominal_targets_pass(request, score, independent_audit=True) is not pointwise
    mean = [e for e in score["errors"] if e.reduction == "mean"]
    assert len(mean) == 1 and mean[0].actual == pytest.approx(.8)
    assert mean[0].wavelength_nm is None and mean[0].normalized_violation == 0.
    assert score["weighted_rms_error"] == 0.
    assert len(score["coverage"][0].wavelength_nm) == 5
    assert len(score["audit_coverage"][0].wavelength_nm) >= 9
    assert all((e.wavelength_min_nm, e.wavelength_max_nm) == (500., 600.) for e in score["errors"])
    if pointwise:
        assert value["audit"]["bracket_count"] > 0
        assert score["max_error"] > .6
    else:
        assert value["audit"]["bracket_count"] == 0
        assert score["max_error"] == 0.


def test_real_tmm_narrow_pointwise_miss_remains_an_audit_gate():
    objective = FilmOptimizationObjective(549., 551., 3, metric="T",
        pointwise=FilmObjectiveConstraint(lower_bound=None, upper_bound=.01))
    request = _frozen_request(objective, glass_index=1.)
    design = deepcopy(dict(request.environment))
    # Synthetic symmetric high-Q cavity; no real-material claim is made.
    center = 550.123456
    mirror = [("high", center / (4 * 2.2)), ("low", center / (4 * 1.3))] * 14
    recipe = mirror + [("low", center / (2 * 1.3))] + list(reversed(mirror))
    design["front_layers"] = [dict(id=f"cavity-{index}", material_id=material, optical_set_id="nk",
        thickness=dict(value=thickness, unit="nm"), note="synthetic audit fixture")
        for index, (material, thickness) in enumerate(recipe)]
    value = audit_frozen_synthesis_candidate(request, design)
    assert max(value["score"]["coverage"][0].actual_values) < .01
    assert complete_original_score(request, value["score"], independent_audit=True)
    assert not nominal_targets_pass(request, value["score"], independent_audit=True)
    assert value["score"]["max_error"] > .9
    assert value["score"]["weighted_rms_error"] == 0.


def _producer_observer(objective):
    packets: list[dict[str, Any]] = []
    tick = iter(range(1000))
    observer = CandidateDisplayObserver(context_id="real-band-producer", objectives=(objective,),
        control_provider=lambda: dict(enabled=True), publish_callback=packets.append,
        clock=lambda: next(tick))
    return observer, packets


@pytest.mark.parametrize("pointwise", [False, True])
def test_real_synthesis_producer_keeps_full_raw_spectrum_without_extra_forward(monkeypatch, pointwise):
    from coat_sim.core import film_optimization_structure as structure
    from coat_sim.core.film_auto_synthesis import synthesize_film_design

    objective = FilmOptimizationObjective(500., 600., 3,
        mean=FilmObjectiveConstraint(lower_bound=None, upper_bound=.05),
        pointwise=FilmObjectiveConstraint(enabled=pointwise, lower_bound=None, upper_bound=.03))
    request = replace(_frozen_request(objective), max_evaluations=2, max_cycles=1,
                      search_strategy="legacy", ar_structure_mode="v23_matching")
    calls = []
    original = structure.simulate_with_thickness_jacobian

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(structure, "simulate_with_thickness_jacobian", counted)
    expected = synthesize_film_design(None, request)
    expected_forwards = len(calls)
    calls.clear()
    observer, packets = _producer_observer(objective)
    actual = synthesize_film_design(None, request, candidate_observer=observer)
    assert expected_forwards > 0 and len(calls) == expected_forwards
    assert (actual.evaluations, actual.validation_evaluations, actual.failed_evaluations) == (
        expected.evaluations, expected.validation_evaluations, expected.failed_evaluations)
    assert actual.candidate_design == expected.candidate_design
    assert observer._top and not observer.last_error
    rows = [packet["current"] for packet in packets if packet["current"] is not None]
    live = next(row for row in rows if row["availability"] == "available")
    assert live["condition_count"] == (4 if pointwise else 1)
    assert live["spectrum"]["original_point_count"] == 3
    assert live["spectrum"]["wavelength_nm"] == [500., 550., 600.]
    assert min(live["spectrum"]["actual"]) > objective.pointwise.upper_bound
    assert live["nominal_pass"] is not pointwise


@pytest.mark.parametrize("pointwise", [False, True])
def test_real_ideal_producer_keeps_full_raw_spectrum_without_extra_forward(monkeypatch, pointwise):
    from coat_sim.core import film_ideal_sensitivity as ideal
    from coat_sim.core import _film_ideal_sensitivity_core as core
    from coat_sim.core.film_objective_criteria import wavelength_measure
    from coat_sim.core.film_optimization import FilmThicknessVariable

    service = _Service()
    design = new_design()
    design["substrate"].update(material_id="glass", optical_set_id="nk")
    design["front_layers"] = [dict(id="high-film", material_id="high", optical_set_id="nk",
        thickness=dict(value=80., unit="nm"), note="real producer display fixture")]
    objective = FilmOptimizationObjective(500., 600., 3)
    request = ideal.FilmIdealSensitivityRequest(design, 0, (objective,),
        (FilmThicknessVariable("front", "high-film", 60., 145.),), "absolute",
        max_evaluations=1, max_iterations=1, policy="strict_nominal", objective_ids=("band",))
    # Derive a band bound between this synthetic layer's physical mean and its
    # peak. The same raw local miss is diagnostic-only when pointwise is disabled.
    probe = ideal.analyze_film_ideal_sensitivity(service, request)
    assert probe.best_analysis is not None
    raw = np.asarray(probe.best_analysis.full_grid_values)
    mean = float(wavelength_measure((500., 550., 600.), 500., 600.) @ raw)
    limit = (mean + float(np.max(raw))) / 2
    assert mean < limit < float(np.max(raw))
    objective = replace(objective,
        mean=FilmObjectiveConstraint(lower_bound=None, upper_bound=limit),
        pointwise=FilmObjectiveConstraint(enabled=pointwise, lower_bound=None, upper_bound=limit))
    request = ideal.freeze_film_ideal_sensitivity_request(service, replace(request, objectives=(objective,)))
    calls = []
    original = core.simulate_with_thickness_jacobian

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(core, "simulate_with_thickness_jacobian", counted)
    expected = ideal.execute_film_ideal_sensitivity_request(None, request)
    expected_forwards = len(calls)
    calls.clear()
    observer, packets = _producer_observer(objective)
    actual = ideal.execute_film_ideal_sensitivity_request(None, request, candidate_observer=observer)
    assert len(calls) == expected_forwards == 1
    assert actual == expected
    assert observer._top and not observer.last_error
    live = next(packet["current"] for packet in packets if packet["current"] is not None
                and packet["current"]["availability"] == "available")
    assert live["condition_count"] == (4 if pointwise else 1)
    assert live["spectrum"]["original_point_count"] == 3
    assert live["spectrum"]["actual"] == raw.tolist()
    assert max(live["spectrum"]["actual"]) > limit
    assert live["nominal_pass"] is not pointwise


def test_missing_active_evidence_is_reported_without_an_invented_mean():
    status = objective_constraint_status((_objective(),), (), 0.)[0]
    assert not status["targets_met"] and not status["solver_acceptable"]
    assert status["mean"]["worst_actual"] is None
    assert status["mean"]["angles"][0]["evidence_error"] == "missing active conditions"
    assert status["pointwise"] == {"enabled": False}
