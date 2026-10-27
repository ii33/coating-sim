"""Automatic layer floor, structural proposals and paired optical evidence."""

from copy import deepcopy
from dataclasses import replace
from importlib import import_module
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from coat_sim.core import film_automatic_repair as automatic
from coat_sim.core import film_automatic_repair_jobs as jobs
from coat_sim.core.film_manufacturing_jobs import request_from_payload
from coat_sim.core.film_objective_criteria import (
    FilmObjectiveConstraint,
    reduce_objective_responses,
)
from coat_sim.core.film_optimization_scoring import reduction_score
from tests.test_film_manufacturing_jobs import request_fixture

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def problem():
    service, payload, _job = request_fixture()
    request, source = request_from_payload(payload)
    return service, request.synthesis, source


def permissive_problem():
    """Keep acceptance feasible when testing handoff, cancellation or topology."""
    service, request, source = problem()
    request = replace(
        request,
        objectives=tuple(
            replace(o, pointwise=replace(o.pointwise, upper_bound=1.0)) for o in request.objectives
        ),
    )
    return service, request, source


def test_layer_lower_limit_is_not_total_or_an_upper_bound():
    _, request, source = problem()
    assert automatic.lower_limit(10.0, 1.0) == 9.0
    assert automatic.lower_limit(10.0, 20.0) == 0.0
    assert automatic.deficient_ids(source, 9.0) == ["defect"]
    candidates = list(automatic.structural_candidates(request, source, 10.0, 9.0))
    assert {name for name, _ in candidates} >= {
        "raise_thin_layers",
        "delete_thin_layers_merge",
        "insert_material_layer",
    }
    assert any(len(d["front_layers"]) > len(source["front_layers"]) for _, d in candidates)
    assert any(len(d["front_layers"]) < len(source["front_layers"]) for _, d in candidates)
    assert candidates[0][0] == "raise_to_lower_limit"
    assert [r["thickness"]["value"] for r in candidates[0][1]["front_layers"]] == [40.0, 9.0, 40.0]
    raised = next(design for name, design in candidates if name == "raise_thin_layers")
    assert [r["thickness"]["value"] for r in raised["front_layers"]] == [40.0, 10.0, 40.0]
    assert source["front_layers"][1]["thickness"]["value"] == 0.5


def test_pointwise_pairing_never_cancels_equal_and_opposite_changes():
    _, request, source = problem()
    objective = replace(
        request.objectives[0],
        wavelength_min_nm=500.0,
        wavelength_max_nm=600.0,
        wavelength_samples=3,
        angles_deg=(0.0, 30.0),
    )
    request = replace(request, objectives=(objective,))

    def physical(_design, **_kwargs):
        return dict(
            design=source,
            responses=[
                dict(
                    objective_index=0,
                    angle_deg=a,
                    wavelength_nm=np.array([500.0, 550.0, 600.0]),
                    actual_values=np.array([0.2, 0.2, 0.2]),
                )
                for a in (0.0, 30.0)
            ],
        )

    pair = automatic.CurvePair(request, source, physical)
    candidate = physical(source)
    candidate["responses"][0]["actual_values"] = np.array([0.3, 0.1, 0.2])
    guide = automatic._guard_request(request, 0.05)
    transformed = pair.transform(guide.objectives, candidate)
    evidence = pair.evidence(0.05)
    assert not evidence["passed"]
    assert evidence["maximum_absolute_change"] == pytest.approx(0.1)
    assert np.mean(candidate["responses"][0]["actual_values"]) == pytest.approx(0.2)
    assert transformed["score"]["nominal_pass"] is False
    assert all(row.reduction == "pointwise" for row in transformed["score"]["errors"])
    assert {
        (r["objective_index"], r["angle_deg"], r["metric"], r["polarization"])
        for r in evidence["records"]
    } == {
        (0, 0.0, objective.metric, objective.polarization),
        (0, 30.0, objective.metric, objective.polarization),
    }


def test_real_automatic_repair_prefers_original_topology_and_rechecks_every_layer():
    _, request, source = permissive_problem()
    original = deepcopy(source)
    result = automatic.automatic_repair(
        request,
        source,
        target_nm=1.0,
        thickness_tolerance_nm=0.1,
        optical_tolerance=0.01,
        policy=dict(version=1, proposals=20, local_iterations=12, physical_calls=500, seconds=20.0),
    )
    assert result["detected_layer_ids"] == ["defect"]
    assert result["best"] is not None
    assert result["best"]["optical_guard"]["passed"]
    assert not automatic.deficient_ids(result["best"]["design"], 0.9)
    assert len(result["best"]["design"]["front_layers"]) == len(source["front_layers"])
    assert result["trace"][0]["operator"] == "raise_to_lower_limit"
    assert source == original
    assert result["source"]["training_score"]["nominal_pass"] is True
    assert result["best"]["training_score"]["nominal_pass"] is True
    assert result["best"]["audit_score"]["nominal_pass"] is True


def test_original_target_failure_is_review_only_even_when_curve_difference_passes(monkeypatch):
    _, request, source = problem()
    published = []

    def unchanged_joint_trial(local, initial, **_kwargs):
        assert automatic._interior_margin_objective(request.objectives[0]) in local.objectives
        assert initial["score"]["nominal_pass"] is False
        return initial, {"termination": "controlled unchanged joint trial"}

    monkeypatch.setattr(automatic, "refine_synthesis_full_minimax", unchanged_joint_trial)
    result = automatic.automatic_repair(
        request,
        source,
        target_nm=1.0,
        thickness_tolerance_nm=0.1,
        optical_tolerance=0.01,
        checkpoint=lambda value: published.append(deepcopy(value)),
        policy=dict(version=1, proposals=1, local_iterations=2, physical_calls=100, seconds=15.0),
    )
    # The real frozen TMM source R is about .239. A .01 curve-difference
    # allowance cannot reach the original .1 upper target.
    assert result["source"]["training_score"]["nominal_pass"] is False
    reviewed = result["reviewed"]
    assert reviewed is not None and reviewed["optical_guard"]["passed"]
    assert not reviewed["training_score"]["nominal_pass"]
    assert not reviewed["audit_score"]["nominal_pass"]
    assert result["best"] is None and not published
    assert result["trace"] and all(row["accepted"] is False for row in result["trace"])


@pytest.mark.parametrize("explicit_mean", [False, True])
def test_joint_local_solver_keeps_only_explicit_mean_and_all_original_criteria(
    monkeypatch, explicit_mean
):
    _, request, source = permissive_problem()
    objective = replace(
        request.objectives[0],
        mean=replace(request.objectives[0].mean, enabled=explicit_mean, upper_bound=0.3),
    )
    request = replace(request, objectives=(objective,))
    inspected = []

    def unchanged_joint_trial(local, initial, **_kwargs):
        protected = automatic._interior_margin_objective(objective)
        assert protected in local.objectives
        active_means = [o.mean for o in local.objectives if o.mean.enabled]
        assert active_means == ([protected.mean] if explicit_mean else [])
        inspected.append(local)
        return initial, {"termination": "controlled unchanged joint trial"}

    monkeypatch.setattr(automatic, "refine_synthesis_full_minimax", unchanged_joint_trial)
    result = automatic.automatic_repair(
        request,
        source,
        target_nm=1.0,
        thickness_tolerance_nm=0.1,
        optical_tolerance=0.01,
        policy=dict(version=1, proposals=1, local_iterations=2, physical_calls=100, seconds=15.0),
    )
    assert inspected and result["best"] is not None
    assert result["best"]["training_score"]["nominal_pass"]
    assert result["best"]["audit_score"]["nominal_pass"]
    assert request.objectives[0].mean.upper_bound == 0.3


@pytest.mark.parametrize(
    "criterion,expected_lower,expected_upper",
    [
        (FilmObjectiveConstraint(lower_bound=0.0, upper_bound=0.004), 0.0, 0.003996),
        (FilmObjectiveConstraint(lower_bound=0.996, upper_bound=1.0), 0.996004, 1.0),
        (FilmObjectiveConstraint(lower_bound=0.0, upper_bound=1.0), 0.0, 1.0),
        (FilmObjectiveConstraint(lower_bound=0.3, upper_bound=0.3), 0.3, 0.3),
        (
            FilmObjectiveConstraint(
                mode="value", target_value=0.3, lower_bound=None, upper_bound=None
            ),
            None,
            None,
        ),
    ],
)
def test_original_guide_margin_never_changes_formal_value_zero_width_or_physical_endpoints(
    criterion,
    expected_lower,
    expected_upper,
):
    _, request, _ = problem()
    original = replace(request.objectives[0], pointwise=criterion)
    guide = automatic._interior_margin_objective(original)
    assert guide.pointwise.lower_bound == expected_lower
    assert guide.pointwise.upper_bound == expected_upper
    assert guide.pointwise.mode == criterion.mode
    assert guide.pointwise.target_value == criterion.target_value
    assert guide.mean == original.mean and not guide.mean.enabled
    assert original.pointwise == criterion


@pytest.mark.parametrize(
    "original_present,training_pass,audit_pass,accepted",
    [
        (True, True, True, True),
        (True, False, True, False),
        (True, True, False, False),
        (False, False, False, True),
    ],
)
def test_acceptance_keeps_original_training_and_audit_separate_from_curve_guard(
    original_present,
    training_pass,
    audit_pass,
    accepted,
):
    guard = dict(passed=True)
    snapshot = dict(
        thickness_pass=True,
        optical_guard=guard,
        training_score=dict(nominal_pass=training_pass),
        audit_score=dict(nominal_pass=audit_pass),
    )
    assert automatic.candidate_accepted(snapshot, original_present) is accepted
    guard["passed"] = False
    assert not automatic.candidate_accepted(snapshot, original_present)


def test_cancel_retains_already_audited_result_without_starting_more_physics():
    _, request, source = permissive_problem()
    cancel = Event()
    frames = []

    def progress(value):
        frames.append(value["stage"])
        if value["stage"] == "repair_audited_best":
            cancel.set()

    result = automatic.automatic_repair(
        request,
        source,
        target_nm=1.0,
        thickness_tolerance_nm=0.1,
        optical_tolerance=0.01,
        cancel_event=cancel,
        report=progress,
    )
    assert result["termination"] == "cancelled"
    assert result["best"] is not None and result["best"]["optical_guard"]["passed"]
    assert frames[-1] == "repair_audited_best"


def test_new_request_contains_only_three_user_parameters_and_rejects_forged_guard(tmp_path):
    service, request, source = permissive_problem()
    payload = jobs.make_request_payload(
        service,
        request,
        source,
        reference_nm=1.0,
        thickness_tolerance_nm=0.1,
        optical_tolerance_percentage_points=1.0,
    )
    assert not {
        "defect_layer_ids",
        "refinement_evaluations_per_route",
        "max_iterations_per_route",
        "seconds_per_route",
    } & set(payload)
    job = jobs.build_job_payload(None, payload)
    result = jobs.execute_job(
        {"payload": job}, Event(), lambda x: None, tmp_path / "checkpoint.json", lambda *x: None
    )
    assert result["result"]["best"] is not None
    for name in ("source", "best"):
        policy = result["result"][name]["audit_score"]["audit"]["absorption_assumption"]
        assert policy["policy"] == "ordinary_simulation"
        assert policy["wavelength_extrapolation"] == payload["comparison_settings"]["extrapolation_policy"]
        assert policy["k_sampling_policy"] == "source_only_zero_defaults"
    assert jobs.validate_result_payload(result, expected_job=job, replay=True) == result
    bad = deepcopy(result)
    bad["result"]["best"]["optical_guard"]["maximum_absolute_change"] = 0.0
    with pytest.raises(ValueError, match="classification"):
        jobs.validate_result_payload(bad, expected_job=job, replay=False)


def test_difference_audit_finds_peak_between_monotone_physical_curves(monkeypatch):
    from coat_sim.core import film_synthesis_audit as audit

    _, request, source = problem()
    objective = replace(
        request.objectives[0],
        wavelength_min_nm=500.0,
        wavelength_max_nm=600.0,
        wavelength_samples=3,
    )
    request = replace(request, objectives=(objective,))
    guide = automatic._guard_request(request, 0.0005)

    def response(objectives, design):
        rows = []
        for i, o in enumerate(objectives):
            waves = np.linspace(o.wavelength_min_nm, o.wavelength_max_nm, o.wavelength_samples)
            base = 0.2 + 0.001 * (waves - 500.0)
            actual = base + (
                0.001 * np.exp(-(((waves - 517.3) / 4.0) ** 2)) if design is not source else 0.0
            )
            for angle in o.angles_deg:
                rows.append(
                    dict(
                        objective_index=i,
                        angle_deg=angle,
                        wavelength_nm=waves,
                        actual_values=actual,
                    )
                )
        return dict(
            design=design,
            responses=rows,
            score=reduction_score(reduce_objective_responses(objectives, rows), 0.0),
        )

    candidate = deepcopy(source)

    def evaluate(proxy, design, **_kwargs):
        return response(proxy.base_request.objectives, design)

    monkeypatch.setattr(audit, "evaluate_frozen_structure_candidate", evaluate)
    pair = automatic.CurvePair(
        guide, source, lambda _design, objectives, **kw: response(objectives, source)
    )
    value = audit.audit_frozen_synthesis_candidate(
        guide, candidate, response_transform=pair.transform
    )
    evidence = pair.evidence(0.0005)
    assert not evidence["passed"] and not value["score"]["nominal_pass"]
    assert evidence["maximum_absolute_change"] == pytest.approx(0.001, rel=1e-4)
    assert any(abs(w - 517.3) < 0.01 for row in evidence["records"] for w in row["wavelength_nm"])


def test_ten_nm_target_two_nm_tolerance_accepts_eight_nm_without_capping_healthy_layers(
    monkeypatch,
):
    _, request, source = permissive_problem()
    for row, thickness in zip(source["front_layers"], (100.0, 7.0, 60.0), strict=True):
        row["thickness"]["value"] = thickness
    original = deepcopy(source)

    def land_on_allowed_lower_edge(_request, initial, *, evaluate, **_kwargs):
        candidate = deepcopy(initial["design"])
        candidate["front_layers"][1]["thickness"]["value"] = 8.0
        return evaluate(candidate), {"termination": "controlled physical lower-edge endpoint"}

    monkeypatch.setattr(automatic, "refine_synthesis_full_minimax", land_on_allowed_lower_edge)
    result = automatic.automatic_repair(
        request,
        source,
        target_nm=10.0,
        thickness_tolerance_nm=2.0,
        optical_tolerance=1.0,
        policy=dict(version=1, proposals=1, local_iterations=5, physical_calls=100, seconds=15.0),
    )
    assert result["detected_layer_ids"] == ["defect"]
    assert result["lower_limit_nm"] == 8.0
    assert result["best"] is not None
    assert [r["thickness"]["value"] for r in result["best"]["design"]["front_layers"]] == [
        100.0,
        8.0,
        60.0,
    ]
    assert source == original


def test_independent_peak_feedback_is_bounded_and_preserves_true_cancel_and_budget_work(
    monkeypatch,
):
    from coat_sim.core import film_synthesis_audit as audit

    _, request, source = permissive_problem()
    objective = replace(
        request.objectives[0],
        wavelength_min_nm=500.0,
        wavelength_max_nm=600.0,
        wavelength_samples=3,
    )
    request = replace(request, objectives=(objective,), max_evaluations=None)
    source_hash = automatic.physical_design_hash(source)
    calls = []
    solver_calls = []

    def evaluate(proxy, design, **_kwargs):
        calls.append(automatic.physical_design_hash(design))
        objectives = proxy.base_request.objectives
        rows: list[dict] = []
        for index, active in enumerate(objectives):
            waves = np.linspace(
                active.wavelength_min_nm, active.wavelength_max_nm, active.wavelength_samples
            )
            values = 0.2 + 0.001 * (waves - 500.0)
            if automatic.physical_design_hash(design) != source_hash:
                values = values + 0.001 * np.exp(-(((waves - 517.3) / 4.0) ** 2))
            rows.extend(
                dict(
                    objective_index=index,
                    angle_deg=angle,
                    wavelength_nm=waves,
                    actual_values=values,
                    jacobian_values_by_layer_id={
                        layer["id"]: np.zeros(len(waves)) for layer in design["front_layers"]
                    },
                )
                for angle in active.angles_deg
            )
        return dict(
            design=design,
            responses=rows,
            score=reduction_score(reduce_objective_responses(objectives, rows), 0.0),
        )

    def unchanged_trial(_request, initial, **_kwargs):
        solver_calls.append(initial)
        return initial, {"termination": "controlled unchanged trial"}

    monkeypatch.setattr(automatic, "evaluate_frozen_structure_candidate", evaluate)
    monkeypatch.setattr(audit, "evaluate_frozen_structure_candidate", evaluate)
    monkeypatch.setattr(automatic, "refine_synthesis_full_minimax", unchanged_trial)

    def run(*, stop_at_feedback=False, budget=500):
        calls.clear()
        solver_calls.clear()
        cancel = Event()
        frames = []

        def progress(frame):
            frames.append(dict(frame, actual_call_count=len(calls)))
            if stop_at_feedback and frame["stage"] == "repair_audit_feedback":
                cancel.set()

        result = automatic.automatic_repair(
            request,
            source,
            target_nm=1.0,
            thickness_tolerance_nm=0.1,
            optical_tolerance=0.0005,
            original_targets_present=False,
            cancel_event=cancel,
            report=progress,
            policy=dict(
                version=1, proposals=1, local_iterations=2, physical_calls=budget, seconds=15.0
            ),
        )
        assert result["work"]["physical_calls"] == len(calls) <= budget
        return result, frames, len(solver_calls)

    result, frames, solved = run()
    assert result["best"] is None and result["reviewed"] is not None
    assert solved == 2 and [row["feedback_rounds"] for row in result["trace"]] == [0, 1]
    first, correction = result["trace"]
    assert 1 <= correction["feedback_points"] <= 8
    assert first["training_difference"]["passed"] is True
    assert first["audit_difference"]["passed"] is False
    assert (
        first["audit_difference"]["maximum_absolute_change"]
        > first["training_difference"]["maximum_absolute_change"]
    )
    worst = first["audit_difference"]["worst_point"]
    assert abs(worst["wavelength_nm"] - 517.3) < 0.01
    assert (
        worst["objective_index"],
        worst["metric"],
        worst["angle_deg"],
        worst["polarization"],
    ) == (0, "R", 0.0, objective.polarization)
    for row in result["trace"]:
        assert row["training_difference"]["candidate_physical_hash"] == row["physical_hash"]
        assert row["audit_difference"]["candidate_physical_hash"] == row["physical_hash"]
        assert row["local_termination"] == "controlled unchanged trial"
        assert row["local_seconds"] >= 0 and row["audit_seconds"] >= 0
    feedback = [frame for frame in frames if frame["stage"] == "repair_audit_feedback"]
    assert len(feedback) == 1
    calls_at_feedback = feedback[0]["actual_call_count"]
    cancelled, cancelled_frames, solved = run(stop_at_feedback=True)
    assert cancelled["termination"] == "cancelled" and solved == 1
    assert cancelled["reviewed"] is not None and cancelled["best"] is None
    assert cancelled["work"]["physical_calls"] == calls_at_feedback
    assert cancelled_frames[-1]["stage"] == "repair_audit_feedback"
    limited, _limited_frames, solved = run(budget=calls_at_feedback)
    assert limited["termination"] == "evaluation_limit" and solved == 1
    assert limited["work"]["physical_calls"] == calls_at_feedback
    assert limited["best"] is None


@pytest.mark.parametrize("changed_layer", ["existing", "inserted"])
def test_every_linked_or_new_layer_is_rechecked_against_the_same_floor(monkeypatch, changed_layer):
    _, request, source = problem()
    published = []

    def invalid_endpoint(_request, initial, **_kwargs):
        candidate = deepcopy(initial["design"])
        if changed_layer == "inserted":
            candidate["front_layers"].append(
                {**deepcopy(candidate["front_layers"][0]), "id": "newly-too-thin"}
            )
        candidate["front_layers"][-1]["thickness"]["value"] = 7.99
        return {**initial, "design": candidate}, {
            "termination": "controlled invalid physical endpoint"
        }

    monkeypatch.setattr(automatic, "refine_synthesis_full_minimax", invalid_endpoint)
    with pytest.raises(ValueError, match="deficient|thickness|lower"):
        automatic.automatic_repair(
            request,
            source,
            target_nm=10.0,
            thickness_tolerance_nm=2.0,
            optical_tolerance=1.0,
            checkpoint=lambda value: published.append(deepcopy(value)),
            policy=dict(
                version=1, proposals=1, local_iterations=5, physical_calls=100, seconds=15.0
            ),
        )
    assert not published


def _scalar_pair(request, source, before=0.5):
    reference = deepcopy(source)
    reference["note"] = repr(before)
    physical_sources = []

    def physical(design, *, objectives, **_kwargs):
        physical_sources.append(deepcopy(design))
        return dict(
            design=design,
            responses=[
                dict(
                    objective_index=i,
                    angle_deg=angle,
                    wavelength_nm=np.linspace(
                        o.wavelength_min_nm, o.wavelength_max_nm, o.wavelength_samples
                    ),
                    actual_values=np.full(o.wavelength_samples, float(design["note"])),
                )
                for i, o in enumerate(objectives)
                for angle in o.angles_deg
            ],
        )

    pair = automatic.CurvePair(request, reference, physical)

    def candidate(actual):
        design = deepcopy(reference)
        design["note"] = repr(actual)
        return dict(
            design=design,
            responses=[
                dict(
                    objective_index=i,
                    angle_deg=angle,
                    wavelength_nm=np.linspace(
                        o.wavelength_min_nm, o.wavelength_max_nm, o.wavelength_samples
                    ),
                    actual_values=np.full(o.wavelength_samples, actual),
                )
                for i, o in enumerate(request.objectives)
                for angle in o.angles_deg
            ],
        )

    return pair, reference, physical_sources, candidate


def test_curve_guard_uses_fixed_run_start_not_rolling_previous_trial_and_next_run_is_fresh():
    _, request, source = problem()
    guide = automatic._guard_request(request, 0.01)
    pair, reference, calls, candidate = _scalar_pair(guide, source)
    pair.transform(guide.objectives, candidate(0.506))
    assert pair.evidence(0.01)["passed"]
    # An external edit and a preceding successful trial cannot move this run's
    # frozen .500 baseline. Two .6pp steps are a cumulative 1.2pp change.
    reference["note"] = ".506"
    pair.transform(guide.objectives, candidate(0.512))
    evidence = pair.evidence(0.01)
    assert not evidence["passed"]
    assert 100 * evidence["maximum_absolute_change"] == pytest.approx(1.2)
    assert len(calls) == 1 and float(calls[0]["note"]) == 0.5
    assert evidence["records"][0]["reference_values"] == [0.5]
    new_pair, _new_reference, new_calls, new_candidate = _scalar_pair(guide, source, before=0.506)
    new_pair.transform(guide.objectives, new_candidate(0.512))
    assert new_pair.evidence(0.01)["passed"]
    assert float(new_calls[0]["note"]) == 0.506


def test_fraction_units_mean_percentage_points_and_zero_tolerance_has_no_floor():
    _, request, source = problem()
    guide = automatic._guard_request(request, 0.01)
    pair, _reference, _calls, candidate = _scalar_pair(guide, source)
    pair.transform(guide.objectives, candidate(0.51))
    assert 100 * pair.evidence(0.01)["maximum_absolute_change"] == pytest.approx(1.0)
    assert pair.evidence(0.01)[
        "passed"
    ]  # .50 -> .51 is exactly one percentage point in displayed inputs.
    jobs._validate_guard(pair.evidence(0.01), guide, 0.01)
    assert not pair.evidence(0.009999)["passed"]
    jobs._validate_guard(pair.evidence(0.009999), guide, 0.009999)
    zero_guide = automatic._guard_request(request, 0.0)
    same, _reference, _calls, same_candidate = _scalar_pair(zero_guide, source)
    same.transform(zero_guide.objectives, same_candidate(0.5))
    assert same.evidence(0.0)["passed"]
    same.transform(zero_guide.objectives, same_candidate(np.nextafter(0.5, 1.0)))
    assert same.evidence(0.0)["maximum_absolute_change"] > 0.0
    assert not same.evidence(0.0)["passed"]
    jobs._validate_guard(same.evidence(0.0), zero_guide, 0.0)


def test_curve_pair_uses_objective_metric_polarization_angle_and_exact_wavelength_keys():
    _, request, source = problem()
    first = replace(
        request.objectives[0],
        wavelength_min_nm=500.0,
        wavelength_max_nm=600.0,
        wavelength_samples=3,
        angles_deg=(0.0, 30.0),
        metric="R",
        polarization="s",
    )
    second = replace(first, metric="T", polarization="p")
    guide = automatic._guard_request(replace(request, objectives=(first, second)), 0.01)

    def physical(design, *, objectives, **_kwargs):
        return dict(
            design=design,
            responses=[
                dict(
                    objective_index=i,
                    angle_deg=angle,
                    wavelength_nm=np.linspace(
                        o.wavelength_min_nm, o.wavelength_max_nm, o.wavelength_samples
                    ),
                    actual_values=np.linspace(0.1, 0.12, o.wavelength_samples)
                    + (0.5 if o.metric == "T" else 0.0)
                    + angle * 0.001,
                )
                for i, o in enumerate(objectives)
                for angle in o.angles_deg
            ],
        )

    pair = automatic.CurvePair(guide, source, physical)
    candidate = physical(source, objectives=guide.objectives)
    candidate["responses"].reverse()  # Response array order is not physical identity.
    pair.transform(guide.objectives, candidate)
    evidence = pair.evidence(0.01)
    assert evidence["passed"] and evidence["maximum_absolute_change"] == 0.0
    assert {(r["metric"], r["polarization"], r["angle_deg"]) for r in evidence["records"]} == {
        ("R", "s", 0.0),
        ("R", "s", 30.0),
        ("T", "p", 0.0),
        ("T", "p", 30.0),
    }
    mismatched = physical(source, objectives=guide.objectives)
    mismatched["responses"][0]["wavelength_nm"][1] += 0.01
    with pytest.raises(ValueError, match="grid|wavelength"):
        pair.transform(guide.objectives, mismatched)


@pytest.mark.parametrize("bad_frame", ["missing_angle", "duplicate_angle"])
def test_partial_or_duplicate_physical_axes_cannot_be_accepted_by_taking_an_intersection(bad_frame):
    _, request, source = problem()
    objective = replace(request.objectives[0], angles_deg=(0.0, 30.0))
    guide = automatic._guard_request(replace(request, objectives=(objective,)), 0.01)
    pair, _reference, _calls, candidate = _scalar_pair(guide, source)
    value = candidate(0.5)
    if bad_frame == "missing_angle":
        value["responses"].pop()
    else:
        value["responses"].append(deepcopy(value["responses"][0]))
    with pytest.raises(ValueError, match="complete|duplicate|coverage|axes|angle|grid|sample"):
        pair.transform(guide.objectives, value)


def test_unavailable_original_curve_never_becomes_a_zero_change_pass():
    _, request, source = problem()
    guide = automatic._guard_request(request, 0.01)

    def unavailable(_design, **_kwargs):
        raise ValueError("reference optical data unavailable")

    pair = automatic.CurvePair(guide, source, unavailable)
    candidate = dict(
        design=source,
        responses=[
            dict(
                objective_index=0,
                angle_deg=0.0,
                wavelength_nm=np.array([550.0]),
                actual_values=np.array([0.5]),
            )
        ],
    )
    with pytest.raises(ValueError, match="reference optical data unavailable"):
        pair.transform(guide.objectives, candidate)
    assert not pair.evidence(0.01)["passed"]


def test_zero_tolerance_unmatched_candidate_is_review_only_and_not_a_global_infeasibility_claim(
    monkeypatch,
):
    _, request, source = problem()
    before = deepcopy(source)
    monkeypatch.setattr(
        automatic,
        "refine_synthesis_full_minimax",
        lambda _request, initial, **_kwargs: (
            initial,
            {"termination": "controlled unmodified local trial"},
        ),
    )
    result = automatic.automatic_repair(
        request,
        source,
        target_nm=10.0,
        thickness_tolerance_nm=2.0,
        optical_tolerance=0.0,
        policy=dict(version=1, proposals=1, local_iterations=1, physical_calls=100, seconds=15.0),
    )
    assert result["best"] is None
    assert result["reviewed"] is not None
    assert not result["reviewed"]["optical_guard"]["passed"]
    assert result["reviewed"]["optical_guard"]["maximum_absolute_change"] > 0.0
    assert result["termination"] in {
        "proposal_quantum_limit",
        "search_quantum_complete",
        "evaluation_limit",
        "time_limit",
    }
    assert source == before


def live_partial_design(thin=7.0):
    """Synthetic geometry shared by v3 rank tests; these scores are not audits."""
    _, request, design = permissive_problem()
    for row, thickness in zip(design["front_layers"], (100., thin, 60.), strict=True):
        row["thickness"]["value"] = thickness
    return request, design


def rank_input(design, *, violation=0.0, difference=0.0):
    return dict(design=design, training_score=dict(max_error=violation),
                audit_score=dict(max_error=violation),
                optical_guard=dict(maximum_absolute_change=difference))


def live_request_fixture(*, request_id="synthetic-live-repair"):
    """Real frozen TMM request for v3 execution/transport tests, never user data."""
    service, request, source = permissive_problem()
    for row, thickness in zip(source["front_layers"], (100., 7., 60.), strict=True):
        row["thickness"]["value"] = thickness
    payload = jobs.make_live_request_payload(service, request, source, request_id=request_id,
        reference_nm=10., thickness_tolerance_nm=2., optical_tolerance_percentage_points=1.)
    return service, payload


def test_live_soft_floor_partial_rank_prefers_thickness_progress_before_smaller_curve_delta():
    _, source = live_partial_design(7.)
    _, first = live_partial_design(7.5)
    _, later = live_partial_design(7.9)
    _, worse = live_partial_design(6.)
    assert automatic.thickness_defects(source, 8.) == dict(sum=.125, max=.125, count=1)
    assert automatic.thickness_defects(first, 8.) == dict(sum=.0625, max=.0625, count=1)
    assert automatic.thickness_defects(later, 8.)["sum"] == pytest.approx(.0125)
    source_rank = automatic.repair_rank(rank_input(source), 8.)
    first_rank = automatic.repair_rank(rank_input(first, difference=.0001), 8.)
    later_rank = automatic.repair_rank(rank_input(later, difference=.0002), 8.)
    worse_rank = automatic.repair_rank(rank_input(worse, difference=0.), 8.)
    assert later_rank < first_rank < source_rank < worse_rank
    assert automatic.thickness_defects(later, 8.)["count"] == 1
    assert automatic.thickness_defects(source, 0.) == dict(sum=0., max=0., count=0)


def test_live_rank_counts_new_deficient_layers_and_never_uses_total_thickness_as_repair_progress():
    _, source = live_partial_design()
    _, added = live_partial_design(7.9)
    added["front_layers"].append({**deepcopy(added["front_layers"][0]), "id": "new-thin",
                                 "thickness": dict(value=1., unit="nm")})
    reduced_total = deepcopy(source)
    reduced_total["front_layers"][0]["thickness"]["value"] = 90.
    assert automatic.thickness_defects(added, 8.)["count"] == 2
    assert automatic.repair_rank(rank_input(added), 8.) > automatic.repair_rank(rank_input(source), 8.)
    assert automatic.repair_rank(rank_input(reduced_total, difference=.001), 8.) > automatic.repair_rank(rank_input(source), 8.)


def test_live_native_delete_merge_generator_unions_real_source_lineage_without_mutating_source():
    request, source = live_partial_design()
    before = deepcopy(source)
    origins = {row["id"]:[row["id"]] for row in source["front_layers"]}
    proposals = list(automatic._live_structures(request, source, origins, source, 10., 8.))
    merged = [(design, mapping) for operator, design, mapping in proposals
              if operator == "delete_layer_merge" and len(design["front_layers"]) == 1]
    assert merged
    for design, mapping in merged:
        row = design["front_layers"][0]
        assert row["thickness"]["value"] == 160.
        assert set(mapping[row["id"]]) == {"left", "right"}
    assert source == before and origins == {row["id"]:[row["id"]] for row in source["front_layers"]}


def test_live_soft_repair_floor_does_not_disable_native_physical_boundaries():
    request, partial = live_partial_design(7.5)
    request = replace(request, minimum_nonzero_thickness_nm=5., max_layer_thickness_nm=110.,
                      max_total_thickness_nm=200.)
    assert automatic.legal_thicknesses(request, partial, 0.)
    assert not automatic.legal_thicknesses(request, partial, 8.)
    too_thin = deepcopy(partial)
    too_thin["front_layers"][1]["thickness"]["value"] = 4.99
    assert not automatic.legal_thicknesses(request, too_thin, 0.)
    too_thick = deepcopy(partial)
    too_thick["front_layers"][0]["thickness"]["value"] = 111.
    assert not automatic.legal_thicknesses(request, too_thick, 0.)
    assert not automatic.legal_thicknesses(replace(request, max_total_thickness_nm=160.), partial, 0.)


def controlled_live_endpoints(monkeypatch, values):
    """Choose endpoints only; production still computes all TMM/audit evidence."""
    optimize = import_module("scipy.optimize")

    def structures(_request, _base, _origins, source, _target, _floor):
        for index, value in enumerate(values):
            candidate = deepcopy(source)
            candidate["front_layers"][1]["thickness"]["value"] = value
            yield "controlled_partial_" + str(index), candidate, {
                row["id"]: [row["id"]] for row in source["front_layers"]}

    def minimize(_fun, point, *, constraints, callback, **_kwargs):
        for constraint in constraints:
            constraint["fun"](point)
        callback(point)
        return SimpleNamespace(x=point.copy(), success=False, nit=1, status=9,
                               message="controlled nonstationary endpoint")

    monkeypatch.setattr(automatic, "_live_structures", structures)
    monkeypatch.setattr(optimize, "minimize", minimize)


def test_live_real_partial_is_applicable_without_full_thickness_or_convergence_and_cancel_adds_no_physics(
    monkeypatch, tmp_path,
):
    _, payload = live_request_fixture()
    job = jobs.build_job_payload(None, payload)
    controlled_live_endpoints(monkeypatch, [7.5, 7.9, 6.])
    cancel = Event()
    frames = []
    stopped_calls = []

    def report(frame):
        if frame.get("stage") != "repair_live":
            return
        frames.append(frame)
        best = frame.get("champion")
        if best is not None and best["design"]["front_layers"][1]["thickness"]["value"] == 7.9:
            stopped_calls.append(frame["work"]["physical_calls"])
            cancel.set()

    reply = jobs.execute_job({"payload": job}, cancel, report, tmp_path / "live.json", lambda *_a, **_k: None)
    assert reply["schema_version"] == 3 and reply["state"] == "cancelled"
    body = reply["result"]
    best = body["best"]
    assert best is not None and best["design"]["front_layers"][1]["thickness"]["value"] == 7.9
    assert best["thickness_pass"] is False and best["converged"] is False
    assessment = best["assessment"]
    assert assessment["thickness_complete"] is False
    assert assessment["optical_protection_pass"] is True
    assert assessment["original_nominal_pass"] is True
    assert assessment["applicable_improvement"] is True
    assert assessment["all_requirements_met"] is False and best["fully_repaired"] is False
    assert jobs.applicable_improvement(best, body["source"], payload)
    assert not jobs.applicable_improvement(body["source"], body["source"], payload)
    assert stopped_calls and body["work"]["physical_calls"] == stopped_calls[0]
    previous = -1
    for frame in frames:
        checked = jobs.validate_live_progress(frame, payload, after_seq=previous)
        previous = checked["seq"]
    assert jobs.validate_result_payload(reply, expected_job=job, replay=False) == reply


@pytest.mark.parametrize("candidate_nm", [7.5, 8.])
@pytest.mark.parametrize("explicit_mean", [False, True])
def test_live_source_nominal_failure_can_improve_under_same_point_protection_without_becoming_nominal_success(
    monkeypatch, tmp_path, candidate_nm, explicit_mean,
):
    service, request, source = permissive_problem()
    for row, d in zip(source["front_layers"], (100., 7., 60.), strict=True):
        row["thickness"]["value"] = d
    proposed = deepcopy(source)
    proposed["front_layers"][1]["thickness"]["value"] = candidate_nm
    def reflectance(design):
        raw = automatic.evaluate_frozen_structure_candidate(automatic._proxy(request), design)
        return float(raw["responses"][0]["actual_values"][0])
    before, after = reflectance(source), reflectance(proposed)
    assert before != after and abs(after - before) < .01
    criterion = (FilmObjectiveConstraint(lower_bound=None, upper_bound=0.) if after < before
                 else FilmObjectiveConstraint(lower_bound=1., upper_bound=None))
    objective = replace(request.objectives[0], pointwise=criterion,
                        mean=replace(criterion, enabled=explicit_mean))
    request = replace(request, objectives=(objective,))
    payload = jobs.make_live_request_payload(service, request, source, reference_nm=10.,
        thickness_tolerance_nm=2., optical_tolerance_percentage_points=1.)
    job = jobs.build_job_payload(None, payload)
    controlled_live_endpoints(monkeypatch, [candidate_nm])
    reply = jobs.execute_job({"payload": job}, Event(), lambda _f: None,
        tmp_path / "source-failed.json", lambda *_a, **_k: None)
    body = reply["result"]
    best = body["best"]
    assert best is not None and body["source"]["training_score"]["nominal_pass"] is False
    assert best["training_score"]["nominal_pass"] is False and best["audit_score"]["nominal_pass"] is False
    assert best["protection_guard"]["kind"] == "per_condition_nonincrease"
    assert len(best["protection_guard"]["means"]) == (2 if explicit_mean else 0)
    assessment = best["assessment"]
    assert assessment["original_nominal_pass"] is False
    assert assessment["optical_protection_pass"] is True
    assert assessment["applicable_improvement"] is True
    assert assessment["thickness_complete"] is (candidate_nm == 8.)
    assert assessment["all_requirements_met"] is False and best["fully_repaired"] is False
    assert jobs.applicable_improvement(best, body["source"], payload)
    frozen, _ = jobs.request_from_payload(payload)
    for field, other in (("candidate_values", "reference_values"),
                         ("reference_values", "candidate_values")):
        forged = deepcopy(best)
        record = forged["protection_guard"]["records"][0]
        assert record[field][0] != record[other][0]
        record[field][0] = record[other][0]
        forged["assessment"] = automatic.assess_live_candidate(
            forged, body["source"], frozen, 8., original_targets_present=True)
        assert forged["assessment"]["applicable_improvement"] is True
        assert not jobs.applicable_improvement(forged, body["source"], payload)
    if candidate_nm == 8.:
        assert reply["termination_reason"] == "thickness_goal_reached_original_targets_unmet"
    bad = deepcopy(best)
    bad["protection_guard"]["records"][0]["reference_values"][0] = 0. if after < before else 1.
    assert not jobs.applicable_improvement(bad, body["source"], payload)


def test_live_optical_improvement_without_any_thin_prefix_progress_is_not_an_applicable_repair(
    monkeypatch, tmp_path,
):
    service, request, source = permissive_problem()
    for row, d in zip(source["front_layers"], (100., 7., 60.), strict=True):
        row["thickness"]["value"] = d
    proposed = deepcopy(source)
    proposed["front_layers"][0]["thickness"]["value"] = 99.9

    def actual(design, frozen=request):
        return automatic.evaluate_frozen_structure_candidate(automatic._proxy(frozen), design)

    source_r = float(actual(source)["responses"][0]["actual_values"][0])
    candidate_r = float(actual(proposed)["responses"][0]["actual_values"][0])
    assert source_r != candidate_r and abs(source_r - candidate_r) < .01
    criterion = (FilmObjectiveConstraint(lower_bound=None, upper_bound=0.) if candidate_r < source_r
                 else FilmObjectiveConstraint(lower_bound=1., upper_bound=None))
    request = replace(request, objectives=(replace(request.objectives[0], pointwise=criterion),))
    payload = jobs.make_live_request_payload(service, request, source, reference_nm=10.,
        thickness_tolerance_nm=2., optical_tolerance_percentage_points=1.)
    job = jobs.build_job_payload(None, payload)
    controlled_live_endpoints(monkeypatch, [])

    def only_optical(_request, _base, _origins, _source, _target, _floor):
        yield "only_optical_improvement", proposed, {r["id"]:[r["id"]] for r in source["front_layers"]}

    monkeypatch.setattr(automatic, "_live_structures", only_optical)
    frames: list[dict] = []
    reply = jobs.execute_job({"payload": job}, Event(), frames.append,
        tmp_path / "only-optical.json", lambda *_a, **_k: None)
    body = reply["result"]
    frozen, _ = jobs.request_from_payload(payload)
    candidate_score = actual(proposed, frozen)["score"]
    assert candidate_score["max_error"] < body["source"]["training_score"]["max_error"]
    assert automatic.thickness_defects(proposed, 8.) == automatic.thickness_defects(source, 8.)
    source_rank = body["source"]["assessment"]["rank"]
    full_tuple = automatic.repair_rank(rank_input(proposed, violation=candidate_score["max_error"],
        difference=abs(source_r - candidate_r)), 8.)
    assert full_tuple < source_rank  # Optical tie-break alone used to look like progress.
    assert body["best"] is None
    assert not any(frame.get("champion") is not None for frame in frames)


def test_live_has_no_old_lifetime_caps_but_keeps_finite_local_work_and_bounded_trace(monkeypatch):
    from coat_sim.core import film_synthesis_audit as audit
    _, payload = live_request_fixture()
    request, source = jobs.request_from_payload(payload)
    request = replace(request, max_evaluations=2, max_cycles=1, max_seconds=1e-6)
    controlled_live_endpoints(monkeypatch, np.linspace(7.01, 7.99, 33))
    # Shrink the obsolete v2 limits as well: live must never consult them.
    for key, value in (("proposals", 1), ("physical_calls", 1), ("seconds", 1e-6)):
        monkeypatch.setitem(automatic.POLICY, key, value)
    ticks = [0.]
    def clock():
        ticks[0] += 61.
        return ticks[0]
    monkeypatch.setattr(automatic, "monotonic", clock)
    actual_calls = [0]
    local_ceilings = []
    physical = automatic.evaluate_frozen_structure_candidate
    audit_physical = audit.evaluate_frozen_structure_candidate
    def counted(*args, **kwargs):
        actual_calls[0] += 1
        return physical(*args, **kwargs)
    def counted_audit(*args, **kwargs):
        actual_calls[0] += 1
        return audit_physical(*args, **kwargs)
    optimize = import_module("scipy.optimize")
    endpoint = optimize.minimize
    def bounded_local(*args, **kwargs):
        local_ceilings.append(kwargs["options"]["maxiter"])
        return endpoint(*args, **kwargs)
    monkeypatch.setattr(automatic, "evaluate_frozen_structure_candidate", counted)
    monkeypatch.setattr(audit, "evaluate_frozen_structure_candidate", counted_audit)
    monkeypatch.setattr(optimize, "minimize", bounded_local)
    controls = dict(automatic.LIVE_POLICY, local_iterations=1, trace_capacity=4)
    result = automatic.automatic_repair_live(request, source, target_nm=10.,
        thickness_tolerance_nm=2., optical_tolerance=.01, policy=controls)
    assert result["termination"] == "frontier_exhausted"
    assert result["elapsed_seconds"] > 60.
    assert result["work"]["candidates"] == 33 > 32
    assert result["work"]["physical_calls"] == actual_calls[0] > 2
    assert len(local_ceilings) == 33 and set(local_ceilings) == {1}
    assert result["trace_total"] >= 33 and len(result["trace"]) <= 4
    assert not {"seconds", "physical_calls", "proposals"} & set(result["policy"])
    assert result["best"] is not None and result["best"]["assessment"]["thickness_complete"] is False


@pytest.mark.parametrize("candidate_nm", [7.5, 8.])
def test_live_without_original_targets_keeps_nominal_not_applicable_and_audits_paired_r_and_t(
    monkeypatch, tmp_path, candidate_nm,
):
    from coat_sim.core.film_design_service import simulation_settings
    service, request, source = permissive_problem()
    for row, d in zip(source["front_layers"], (100., 7., 60.), strict=True):
        row["thickness"]["value"] = d
    settings = simulation_settings(dict(wavelength_min_nm=500., wavelength_max_nm=600., samples=3,
        angle_deg=25., polarization="p", extrapolation_policy="endpoint_hold"))
    request = replace(request, objectives=tuple(replace(request.objectives[0],
        wavelength_min_nm=500., wavelength_max_nm=600., wavelength_samples=3,
        angles_deg=(25.,), polarization="p", metric=metric,
        pointwise=FilmObjectiveConstraint(lower_bound=0., upper_bound=1.)) for metric in ("R", "T")))
    payload = jobs.make_live_request_payload(service, request, source, reference_nm=10.,
        thickness_tolerance_nm=2., optical_tolerance_percentage_points=1.,
        comparison_settings=settings, original_targets_present=False)
    controlled_live_endpoints(monkeypatch, [candidate_nm])
    job = jobs.build_job_payload(None, payload)
    reply = jobs.execute_job({"payload": job}, Event(), lambda _f: None,
        tmp_path / "no-original.json", lambda *_a, **_k: None)
    best = reply["result"]["best"]
    assert best is not None and jobs.applicable_improvement(best, reply["result"]["source"], payload)
    assert best["assessment"]["original_nominal_pass"] is None
    assert best["assessment"]["optical_protection_pass"] is True
    assert best["assessment"]["all_requirements_met"] is (candidate_nm == 8.)
    assert best["protection_guard"]["kind"] == "not_applicable"
    assert {(row["metric"], row["angle_deg"], row["polarization"]) for row in best["optical_guard"]["records"]} == {
        ("R", 25., "p"), ("T", 25., "p")}
    assert all(not objective.mean.enabled for objective in request.objectives)


@pytest.mark.parametrize("formal_curve_pass", [True, False])
def test_live_positive_numerical_guide_q_is_not_a_substitute_for_formal_optical_acceptance(
    monkeypatch, tmp_path, formal_curve_pass,
):
    from coat_sim.core import film_synthesis_multispectral_minimax as minimax
    service, request, source = permissive_problem()
    for row, d in zip(source["front_layers"], (100., 7., 60.), strict=True):
        row["thickness"]["value"] = d
    payload = jobs.make_live_request_payload(service, request, source, reference_nm=10.,
        thickness_tolerance_nm=2., optical_tolerance_percentage_points=1. if formal_curve_pass else 0.)
    controlled_live_endpoints(monkeypatch, [7.5])
    original_bounds = minimax._signed_bounds
    reported_q = []

    def positive_guide(*args, **kwargs):
        q, jacobian, mass = original_bounds(*args, **kwargs)
        q = q.copy()
        q[int(np.argmax(q))] = max(float(np.max(q)), 1e-15)
        reported_q.append(float(np.max(q)))
        return q, jacobian, mass

    # Only the numerical guide residual is controlled. The candidate's physical
    # responses, declared tolerance and all independent evidence remain real.
    monkeypatch.setattr(minimax, "_signed_bounds", positive_guide)
    job = jobs.build_job_payload(None, payload)
    reply = jobs.execute_job({"payload": job}, Event(), lambda _f: None,
        tmp_path / "positive-guide.json", lambda *_a, **_k: None)
    assert reported_q and all(value > 0. for value in reported_q)
    best = reply["result"]["best"]
    if formal_curve_pass:
        assert best is not None
        assert best["optical_guard"]["passed"] is True
        assert jobs.applicable_improvement(best, reply["result"]["source"], payload)
    else:
        assert best is None  # Zero formal optical tolerance has no implicit floor.


@pytest.mark.parametrize("source_actual", [.004, .0039999999])
def test_live_original_guide_cushion_is_capped_by_source_slack_and_keeps_formal_bound(source_actual):
    _, request, _ = problem()
    objective = replace(request.objectives[0], pointwise=FilmObjectiveConstraint(lower_bound=None, upper_bound=.004))
    request = replace(request, objectives=(objective,))
    score = dict(nominal_pass=True, errors=[dict(objective_index=0, reduction="pointwise", actual=source_actual)])
    source = dict(training_score=score, audit_score=deepcopy(score))
    guide = automatic._live_protection_guide(request, source, True)
    upper = guide.objectives[0].pointwise.upper_bound
    assert source_actual <= upper <= .004
    assert .004 - upper <= (.004 - source_actual) * .5 + np.spacing(.004)
    if source_actual == .004:
        assert upper == .004
    assert request.objectives[0].pointwise.upper_bound == .004
    source["audit_score"]["nominal_pass"] = False
    assert automatic._live_protection_guide(request, source, True) is request
    assert automatic._live_protection_guide(request, source, False) is request


@pytest.mark.parametrize("cancel_at_feedback", [False, True])
def test_live_independent_peak_feedback_is_bounded_and_cancellation_adds_no_physics(
    monkeypatch, cancel_at_feedback,
):
    from coat_sim.core import film_synthesis_audit as audit
    optimize = import_module("scipy.optimize")

    _, request, source = permissive_problem()
    objective = replace(request.objectives[0], wavelength_min_nm=500.,
        wavelength_max_nm=600., wavelength_samples=3)
    request = replace(request, objectives=(objective,))
    source_hash = automatic.physical_design_hash(source)
    calls = []
    local_calls = []
    selected = []
    cancel = Event()

    def physical(proxy, design, **_kwargs):
        calls.append(automatic.physical_design_hash(design))
        objectives = proxy.base_request.objectives
        rows: list[dict] = []
        for index, active in enumerate(objectives):
            waves = np.linspace(active.wavelength_min_nm, active.wavelength_max_nm,
                                active.wavelength_samples)
            values = .2 + .001 * (waves - 500.)
            if automatic.physical_design_hash(design) != source_hash:
                values = values + .001 * np.exp(-(((waves - 517.3) / 4.) ** 2))
            rows.extend(dict(objective_index=index, angle_deg=angle, wavelength_nm=waves,
                actual_values=values, jacobian_values_by_layer_id={
                    row["id"]: np.zeros(len(waves)) for row in design["front_layers"]})
                for angle in active.angles_deg)
        return dict(design=design, responses=rows,
                    score=reduction_score(reduce_objective_responses(objectives, rows), 0.))

    monkeypatch.setattr(automatic, "evaluate_frozen_structure_candidate", physical)
    monkeypatch.setattr(audit, "evaluate_frozen_structure_candidate", physical)
    controlled_live_endpoints(monkeypatch, [7.5])
    endpoint = optimize.minimize

    def finite_local(*args, **kwargs):
        local_calls.append(kwargs["options"]["maxiter"])
        return endpoint(*args, **kwargs)

    selector = automatic._audit_feedback

    def feedback(*args, **kwargs):
        points = selector(*args, **kwargs)
        selected.append((len(points), len(calls)))
        if cancel_at_feedback:
            cancel.set()
        return points

    monkeypatch.setattr(optimize, "minimize", finite_local)
    monkeypatch.setattr(automatic, "_audit_feedback", feedback)
    result = automatic.automatic_repair_live(request, source, target_nm=10.,
        thickness_tolerance_nm=2., optical_tolerance=.0005,
        original_targets_present=False, cancel_event=cancel,
        policy=dict(automatic.LIVE_POLICY, local_iterations=2, feedback_rounds=1))
    assert len(selected) == 1 and 1 <= selected[0][0] <= 8
    assert result["best"] is None and result["reviewed"] is not None
    assert result["reviewed"]["optical_guard"]["passed"] is False
    assert result["reviewed"]["optical_guard"]["maximum_absolute_change"] > .0005
    assert result["work"]["physical_calls"] == len(calls)
    local = [row for row in result["trace"] if row["kind"] == "local_solver"]
    assert len(local) == 1
    if cancel_at_feedback:
        assert result["termination"] == "cancelled" and local_calls == [2]
        assert len(calls) == selected[0][1]
        assert local[0]["termination"] == "cancelled"
    else:
        assert result["termination"] == "frontier_exhausted" and local_calls == [2, 2]
        assert local[0]["feedback_rounds"] == 1
        assert local[0]["feedback_points"] == selected[0][0]


def test_live_real_worse_optics_cannot_be_hidden_by_forged_nonincrease_records(monkeypatch):
    from coat_sim.core.film_manufacturing_jobs import _json
    service, request, source = permissive_problem()
    for row, thickness in zip(source["front_layers"], (100., 7., 60.), strict=True):
        row["thickness"]["value"] = thickness
    candidate = deepcopy(source)
    candidate["front_layers"][1]["thickness"]["value"] = 7.5

    def actual(design):
        value = automatic.evaluate_frozen_structure_candidate(automatic._proxy(request), design)
        return float(value["responses"][0]["actual_values"][0])

    before, after = actual(source), actual(candidate)
    assert before != after and abs(after - before) < .01
    # Choose a genuinely failing original condition that this physical candidate worsens.
    condition = (FilmObjectiveConstraint(lower_bound=None, upper_bound=0.) if after > before
                 else FilmObjectiveConstraint(lower_bound=1., upper_bound=None))
    request = replace(request, objectives=(replace(request.objectives[0], pointwise=condition),))
    # Capture complete physical measurements without discarding the known bad
    # candidate at the enabled-original training prefilter. Scores still use
    # the exact failing condition; only the later static proof is fault-injected.
    payload = jobs.make_live_request_payload(service, request, source,
        reference_nm=10., thickness_tolerance_nm=2., optical_tolerance_percentage_points=1.)
    frozen, source = jobs.request_from_payload(payload)
    controlled_live_endpoints(monkeypatch, [7.5])
    measured = _json(automatic.automatic_repair_live(frozen, source,
        target_nm=10., thickness_tolerance_nm=2., optical_tolerance=.01,
        original_targets_present=False, request_identity={key: value for key, value
            in jobs._live_identity(payload).items() if key != "source_hash"}))
    retained = measured["best"]
    assert retained is not None and retained["optical_guard"]["passed"] is True
    assert retained["training_score"]["max_error"] > measured["source"]["training_score"]["max_error"]
    assert retained["audit_score"]["max_error"] > measured["source"]["audit_score"]["max_error"]

    original, bad = deepcopy(measured["source"]), deepcopy(retained)
    identity = jobs._live_identity(payload)
    for snapshot in (original, bad):
        snapshot.update({key: identity[key] for key in ("request_hash", "source_fingerprint", "source_hash")})
    original["protection_guard"] = dict(kind="baseline_identity", records=[], means=[], passed=True)
    original["assessment"] = automatic.assess_live_candidate(original, original, frozen, 8.)
    bad["protection_guard"] = dict(kind="per_condition_nonincrease",
        records=deepcopy(bad["optical_guard"]["records"]), means=[], passed=False)
    bad["assessment"] = automatic.assess_live_candidate(bad, original, frozen, 8.)
    assert bad["assessment"]["applicable_improvement"] is False
    assert not jobs.applicable_improvement(bad, original, payload)

    for record in bad["protection_guard"]["records"]:
        record["candidate_values"] = deepcopy(record["reference_values"])
    bad["protection_guard"]["passed"] = True
    bad["assessment"] = automatic.assess_live_candidate(bad, original, frozen, 8.)
    assert bad["assessment"]["applicable_improvement"] is True  # The forged scalar assessment alone is unsafe.
    assert not jobs.applicable_improvement(bad, original, payload)


@pytest.mark.parametrize("growth", ["points", "external", "cancel_with_external"])
def test_live_audit_checks_new_points_and_external_owners_and_preserves_user_cancel(monkeypatch, growth):
    from coat_sim.core import film_manufacturing_jobs as manufacturing

    _, payload = live_request_fixture()
    request, source = jobs.request_from_payload(payload)
    request = replace(request, resource_guard_resolution={"effective_bytes":100_000})
    cancel = Event()
    external = [0]
    scans = []
    calls = []
    physical = automatic.evaluate_frozen_structure_candidate

    def actual(proxy, design, **kwargs):
        calls.append(automatic.physical_design_hash(design))
        return physical(proxy, design, **kwargs)

    def owned(*_args):
        scans.append(True)
        return 0

    def audited(active, design, *, progress_callback, retained_callback, **_kwargs):
        # Isolate the accounting boundary with a one-point pending grid. Every
        # counted evaluation still executes real TMM; no audit result is forged.
        retained_callback({})
        for index in range(2):
            progress_callback(dict(physical_evaluations=index, wavelength_points=index))
            actual(automatic._proxy(active), design)
            progress_callback(dict(physical_evaluations=index + 1, wavelength_points=index + 1))
            if growth != "points":
                external[0] = 100_001
            if growth == "cancel_with_external":
                cancel.set()
                raise automatic._StructureCancelled()
            retained_callback({})
        pytest.fail("resource admission did not stop growing audit state")

    monkeypatch.setattr(automatic, "_owned_bytes", owned)
    monkeypatch.setattr(automatic, "manufacturing_refinement_working_set_bytes", lambda *_a: 0)
    monkeypatch.setattr(manufacturing, "estimate_audit_resources", lambda *_a, **_k:
        dict(working_set_bytes=0, wavelength_points_upper_bound=1))
    monkeypatch.setattr(automatic, "evaluate_frozen_structure_candidate", actual)
    monkeypatch.setattr(automatic, "audit_frozen_synthesis_candidate", audited)
    result = automatic.automatic_repair_live(request, source, target_nm=10.,
        thickness_tolerance_nm=2., optical_tolerance=.01, cancel_event=cancel,
        external_retained_bytes=lambda: external[0])
    assert result["termination"] == ("cancelled" if growth == "cancel_with_external" else "resource_guard")
    assert result["best"] is None and result["source"] is None
    assert result["work"]["physical_calls"] == len(calls) == (3 if growth == "points" else 2)
    assert result["work"]["wavelength_points"] == (3 if growth == "points" else 2)
    assert len(scans) >= 4  # Includes the full owned-state scan restored in finally.
