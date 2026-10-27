"""Automatic layer floor, structural proposals and paired optical evidence."""

from copy import deepcopy
from dataclasses import replace
from threading import Event

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
