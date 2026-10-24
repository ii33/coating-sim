"""Complete band constraints agree across real forward and analytic solvers."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from coat_sim.core.film_objective_criteria import reduce_objective_responses
from coat_sim.core.film_optimization import (
    FilmObjectiveConstraint, FilmOptimizationObjective, FilmOptimizationRequest,
    FilmThicknessVariable, optimize_film_design,
)
from coat_sim.core.film_optimization_needle import _normalize_objectives, _score_design
from coat_sim.core.film_optimization_structure import (
    FilmStructureOptimizationRequest, _PreparedStructureEvaluator,
    evaluate_frozen_structure_candidate,
)
from tests.film_performance_fixtures import fixture

pytestmark = pytest.mark.algorithm


def constraint(*, target=None, upper=None, enabled=True):
    return FilmObjectiveConstraint(
        enabled=enabled, mode="value" if target is not None else "range",
        target_value=target, lower_bound=None, upper_bound=upper,
    )


def problem(*, pointwise=True, mean=True, angles=(0., 33.), samples=7):
    service, design, frozen = fixture(layers=2)
    objective = FilmOptimizationObjective(
        450., 750., samples, angles_deg=angles, metric="R", polarization="p",
        pointwise=constraint(target=.05, enabled=pointwise),
        mean=constraint(target=.05, enabled=mean), weight=3., normalization_scale=.4,
    )
    base = FilmOptimizationRequest(
        deepcopy(design), 1, (FilmThicknessVariable("front", "layer-0", 45., 120.),),
        (objective,), max_iterations=1, max_evaluations=1,
        frozen_material_snapshot=deepcopy(frozen), acceptable_normalized_error=0.,
    )
    return service, design, base, FilmStructureOptimizationRequest(base)


@pytest.mark.parametrize("pointwise,mean", [(True, False), (False, True), (True, True)])
def test_forward_baseline_needle_and_prepared_structure_use_same_band_score(pointwise, mean):
    service, design, base, request = problem(pointwise=pointwise, mean=mean)
    structure = evaluate_frozen_structure_candidate(request, design)
    prepared = _PreparedStructureEvaluator(request).evaluate(design)
    needle = _score_design(service, design, base.objectives, acceptable=0.)
    ordinary = optimize_film_design(service, base)
    for score in (prepared["score"], needle):
        assert score["weighted_rms_error"] == pytest.approx(structure["score"]["weighted_rms_error"], rel=1e-12)
        assert score["max_error"] == pytest.approx(structure["score"]["max_error"], rel=1e-12)
        assert score["errors"] == structure["score"]["errors"]
        assert len(score["coverage"]) == 2
        assert all(len(row.wavelength_nm) == 7 for row in score["coverage"])
    assert ordinary.weighted_rms_error == pytest.approx(needle["weighted_rms_error"], rel=1e-12)
    assert ordinary.max_normalized_error == pytest.approx(needle["max_error"], rel=1e-12)
    assert ordinary.per_condition_errors == needle["errors"]
    assert len(needle["errors"]) == 2 * (7 * pointwise + mean)
    if not pointwise:
        assert all(row.wavelength_nm is None and row.reduction == "mean" for row in needle["errors"])


def test_mean_only_can_pass_while_disabled_pointwise_has_local_violations():
    service, design, base, request = problem(pointwise=False, mean=True, angles=(0.,))
    raw = evaluate_frozen_structure_candidate(request, design)["responses"][0]
    waves, actual = np.asarray(raw["wavelength_nm"]), np.asarray(raw["actual_values"])
    mean = float(np.trapezoid(actual, waves) / (waves[-1] - waves[0]))
    threshold = mean + 1e-8
    assert np.max(actual) > threshold
    objective = replace(base.objectives[0], pointwise=constraint(upper=threshold, enabled=False),
                        mean=constraint(upper=threshold))
    mean_score = _score_design(service, design, (objective,), acceptable=0.)
    assert mean_score["max_error"] == 0.
    assert mean_score["nominal_pass"] and mean_score["solver_acceptable"]
    assert len(mean_score["errors"]) == 1
    both = _score_design(service, design, (replace(objective, pointwise=replace(objective.pointwise, enabled=True)),), acceptable=0.)
    assert both["max_error"] > 0.
    assert not both["nominal_pass"]


@pytest.mark.parametrize("pointwise,mean", [(False, True), (True, True)])
def test_multiangle_structure_gradient_matches_full_band_finite_difference(pointwise, mean):
    _service, design, _base, request = problem(pointwise=pointwise, mean=mean)
    result = _PreparedStructureEvaluator(request).evaluate(
        design, variable_ids=("layer-0", "layer-1"), with_jacobian=True)
    step = 1e-4
    for index in range(2):
        lower, upper = deepcopy(design), deepcopy(design)
        lower["front_layers"][index]["thickness"]["value"] -= step
        upper["front_layers"][index]["thickness"]["value"] += step
        lo = evaluate_frozen_structure_candidate(request, lower, variable_ids=("layer-0",), with_jacobian=True)
        hi = evaluate_frozen_structure_candidate(request, upper, variable_ids=("layer-0",), with_jacobian=True)
        finite = (hi["smooth_loss"] - lo["smooth_loss"]) / (2 * step)
        assert result["gradient_by_layer_id"][f"layer-{index}"] == pytest.approx(finite, rel=2e-6, abs=1e-10)


@pytest.mark.parametrize("pointwise,mean", [(False, True), (True, True)])
def test_trf_residual_jacobian_matches_fd_and_reuses_same_point_cache(monkeypatch, pointwise, mean):
    from importlib import import_module
    from coat_sim.core import film_optimization_derivatives as derivatives
    service, _design, base, _request = problem(pointwise=pointwise, mean=mean)
    calls = []
    original = derivatives.simulate_with_thickness_jacobian
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(derivatives, "simulate_with_thickness_jacobian", counted)
    def least_squares(fun, x0, *, jac, callback, **_kwargs):
        before = len(calls)
        residual = fun(x0)
        matrix = jac(x0)
        np.testing.assert_array_equal(fun(x0), residual)
        np.testing.assert_array_equal(jac(x0), matrix)
        assert len(calls) == before
        assert len(residual) == 2 * (7 * pointwise + mean)
        step = 1e-6
        finite = (fun(x0 + step) - fun(x0 - step)) / (2 * step)
        np.testing.assert_allclose(matrix[:, 0], finite, rtol=3e-5, atol=1e-9)
        return SimpleNamespace(success=True, status=1)
    monkeypatch.setattr(import_module("scipy.optimize"), "least_squares", least_squares)
    result = optimize_film_design(service, replace(base, solver="trf", max_evaluations=8))
    assert result.failed_evaluations == 0
    assert len(calls) == 6  # Three distinct full spectra at two independent angles.


def test_normalization_preserves_independent_constraints_and_rejects_both_disabled():
    _service, _design, base, _request = problem(pointwise=False, mean=True)
    objective = replace(base.objectives[0], mean=constraint(upper=.3))
    normalized, = _normalize_objectives((objective,))
    assert normalized.mean == objective.mean
    assert normalized.pointwise == objective.pointwise
    with pytest.raises(ValueError, match="enabled"):
        _normalize_objectives((replace(objective, mean=replace(objective.mean, enabled=False)),))
    legacy_kwargs: dict[str, Any] = {"target_value": .3}
    with pytest.raises(TypeError):
        FilmOptimizationObjective(450., 700., 7, **legacy_kwargs)


def test_single_wavelength_mean_and_pointwise_have_equal_mass_and_response():
    objective = FilmOptimizationObjective(550., 550., 1,
        pointwise=constraint(target=.2), mean=constraint(target=.2), weight=7.)
    reduced = reduce_objective_responses((objective,), [{
        "objective_index": 0, "angle_deg": 0., "wavelength_nm": [550.], "actual_values": [.4],
    }])
    assert reduced.weighted_rms_error == pytest.approx(.2)
    assert reduced.conditions[0].actual == reduced.conditions[1].actual == .4
    np.testing.assert_allclose(reduced.condition_masses, [.5, .5])


def test_ordinary_result_separates_nominal_targets_from_solver_tolerance():
    service, _design, base, _request = problem(pointwise=False, mean=True)
    result = optimize_film_design(service, replace(base, acceptable_normalized_error=1.))
    assert not result.targets_met
    assert result.solver_acceptable
    assert not result.validation_targets_met
    assert result.validation_solver_acceptable
    assert len(result.raw_coverage) == len(result.validation_raw_coverage) == 2
    assert all(len(row.wavelength_nm) == 7 for row in result.raw_coverage)


def test_chunked_forward_reduces_global_grid_once(monkeypatch):
    from coat_sim.core import film_optimization as ordinary
    from coat_sim.core import film_optimization_needle as needle
    service, design, base, _request = problem(pointwise=True, mean=True, samples=8)
    expected = _score_design(service, design, base.objectives, acceptable=0.)
    def small_chunks(waves):
        for start in range(0, len(waves), 3):
            yield waves[start:start + 3]
    monkeypatch.setattr(ordinary, "_wavelength_chunks", small_chunks)
    monkeypatch.setattr(needle, "_wavelength_chunks", small_chunks)
    chunked = _score_design(service, design, base.objectives, acceptable=0.)
    result = optimize_film_design(service, base)
    assert chunked["weighted_rms_error"] == pytest.approx(expected["weighted_rms_error"], rel=1e-12)
    assert result.weighted_rms_error == pytest.approx(expected["weighted_rms_error"], rel=1e-12)
    assert len(chunked["coverage"]) == 2
    assert all(len(row.wavelength_nm) == 8 for row in result.raw_coverage)


@pytest.mark.parametrize("solver", ["baseline", "trf", "lbfgsb", "multistart", "differential_evolution"])
def test_real_small_ordinary_solver_result_matches_shared_final_score(solver):
    service, _design, base, _request = problem(pointwise=False, mean=True)
    result = optimize_film_design(service, replace(base, solver=solver, max_evaluations=8))
    exact = _score_design(service, dict(result.best_design), base.objectives, acceptable=0.)
    assert result.weighted_rms_error == pytest.approx(exact["weighted_rms_error"], rel=1e-12)
    assert result.targets_met == exact["nominal_pass"]
    assert result.solver_acceptable == exact["solver_acceptable"]
    assert result.raw_coverage == exact["coverage"]
    assert len(result.per_condition_errors) == 2


@pytest.mark.parametrize("pointwise", [False, True])
def test_real_short_needle_search_selects_with_full_band_criteria(pointwise):
    from hashlib import sha256
    import json
    from coat_sim.core.film_optimization_needle import (
        FilmNeedleMaterialReference, FilmNeedleOptimizationRequest,
        optimize_film_design_with_needle,
    )
    service, _design, base, _request = problem(pointwise=pointwise, mean=True)
    # The controlled performance fixture intentionally marks all records as
    # unavailable for new design work. Explicitly enable the selected test film.
    frozen = deepcopy(base.frozen_material_snapshot)
    next(row for row in frozen["materials"] if row["id"] == "high")["design_ready"] = True
    frozen["sha256"] = sha256(json.dumps(
        {key: value for key, value in frozen.items() if key != "sha256"},
        sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    base = replace(base, variables=(), max_iterations=2, max_evaluations=8,
                   frozen_material_snapshot=frozen)
    request = FilmNeedleOptimizationRequest(
        base, candidate_materials=(FilmNeedleMaterialReference("high", "optical"),),
        min_thickness_nm=10., max_thickness_nm=30., insertion_trials_per_interface=1,
    )
    result = optimize_film_design_with_needle(service, request).base_result
    exact = _score_design(service, dict(result.best_design), base.objectives, acceptable=0.)
    assert result.evaluations <= 8
    assert result.weighted_rms_error == pytest.approx(exact["weighted_rms_error"], rel=1e-12)
    assert result.raw_coverage == exact["coverage"]
    assert len(result.per_condition_errors) == 2 * (7 * pointwise + 1)


@pytest.mark.parametrize("pointwise,mean", [(False, True), (True, False), (True, True)])
def test_current_job_wire_replays_complete_raw_grids_and_active_constraints(pointwise, mean):
    import json
    from threading import Event
    from coat_sim.core.film_optimization_job import (
        build_job_payload, execute_film_optimization_job, validate_checkpoint,
    )
    service, _design, base, _request = problem(pointwise=pointwise, mean=mean)
    base = replace(base, acceptable_normalized_error=1.)
    checkpoints: list[dict[str, Any]] = []
    result = execute_film_optimization_job(service, build_job_payload(base),
        cancel_event=Event(), checkpoint_callback=checkpoints.append)
    assert result["schema_version"] == 2
    assert len(checkpoints) == 1
    assert result["candidate"]["quality"]["targets_met"] is False
    assert result["candidate"]["quality"]["solver_acceptable"] is True
    for wire in (result, *checkpoints):
        restored = json.loads(json.dumps(wire))
        checked = validate_checkpoint(service, restored)
        assert len(checked["candidate"]["per_condition_errors"]) == 2 * (7 * pointwise + mean)
        assert all(len(row["wavelength_nm"]) == 7 for row in checked["candidate"]["raw_coverage"])
    metadata = result["solver_validation"]
    assert metadata["validation_targets_met"] is False
    assert metadata["validation_solver_acceptable"] is True
    assert all(len(row["wavelength_nm"]) == 13 for row in metadata["validation_raw_coverage"])


@pytest.mark.parametrize("mutation", ["missing_raw", "sparse_raw", "wrong_mean", "wrong_flag", "old_schema", "flat_objective"])
def test_mean_job_replay_rejects_forged_or_legacy_evidence(mutation):
    import json
    from threading import Event
    from coat_sim.core.film_optimization_job import (
        build_job_payload, execute_film_optimization_job, validate_checkpoint,
    )
    service, _design, base, _request = problem(pointwise=False, mean=True)
    wire = execute_film_optimization_job(service, build_job_payload(base), cancel_event=Event())
    forged = json.loads(json.dumps(wire))
    if mutation == "missing_raw":
        del forged["candidate"]["raw_coverage"]
    elif mutation == "sparse_raw":
        row = forged["candidate"]["raw_coverage"][0]
        row["wavelength_nm"], row["actual_values"] = row["wavelength_nm"][3:4], row["actual_values"][3:4]
    elif mutation == "wrong_mean":
        forged["candidate"]["per_condition_errors"][0]["actual"] += .01
    elif mutation == "wrong_flag":
        forged["candidate"]["quality"]["targets_met"] = True
    elif mutation == "old_schema":
        forged["schema_version"] = 1
    else:
        objective = forged["request"]["objectives"][0]
        del objective["mean"], objective["pointwise"]
        objective.update(mode="value", target_value=.05, lower_bound=None, upper_bound=None)
    with pytest.raises(ValueError):
        validate_checkpoint(service, forged)


def test_meanonly_replay_calls_physics_for_all_wavelengths_and_angles():
    from types import MethodType
    from coat_sim.core.film_optimization_job import _replay_candidate_scores
    service, design, base, _request = problem(pointwise=False, mean=True)
    original = service.simulate
    sampled = []
    def counted(_self, candidate, settings):
        sampled.append((settings["angle_deg"], settings["samples"]))
        return original(candidate, settings)
    service.simulate = MethodType(counted, service)
    replay = _replay_candidate_scores(service, design, base.objectives)
    assert sampled == [(0., 7), (33., 7)]
    assert len(replay["per_condition_errors"]) == 2
    assert all(row["wavelength_nm"] is None for row in replay["per_condition_errors"])


def test_current_request_wire_rejects_missing_solver_and_noncanonical_single_wavelength():
    from coat_sim.core.film_optimization_job import deserialize_request, serialize_request
    _service, _design, base, _request = problem()
    payload = serialize_request(base)
    del payload["solver"]
    with pytest.raises(ValueError):
        deserialize_request(payload)
    payload = serialize_request(base)
    payload["objectives"][0].update(wavelength_max_nm=450., wavelength_samples=7)
    with pytest.raises(ValueError):
        deserialize_request(payload)


def test_structure_current_wire_meanonly_roundtrip_and_independent_replay():
    import json
    from coat_sim.core.film_optimization_structure import (
        film_structure_result_from_payload, film_structure_result_to_payload,
        optimize_film_design_with_structure, validate_film_structure_payload,
    )
    service, _design, base, request = problem(pointwise=False, mean=True)
    request = replace(request, mode="growth_only", candidate_budget=1, position_samples=1,
                      base_request=replace(base, max_evaluations=3))
    result = optimize_film_design_with_structure(service, request)
    payload = json.loads(json.dumps(film_structure_result_to_payload(request, result)))
    restored = film_structure_result_from_payload(payload)
    assert len(restored.base_result.per_condition_errors) == 2
    assert restored.base_result.raw_coverage == result.base_result.raw_coverage
    assert validate_film_structure_payload(service, payload)["partial_result"]["candidate_verified"]
    payload["candidate"]["raw_coverage"][0]["actual_values"][3] += .01
    with pytest.raises(ValueError):
        validate_film_structure_payload(service, payload)


def test_completed_structure_cancel_checks_saved_raw_grid_without_new_tmm(monkeypatch):
    import json
    from coat_sim.core import film_optimization_structure as structure
    from coat_sim.core.film_optimization_job import validate_completed_structure_cancel_result
    service, _design, base, request = problem(pointwise=False, mean=True)
    request = replace(request, mode="growth_only", candidate_budget=1, position_samples=1,
                      base_request=replace(base, max_evaluations=3))
    result = structure.optimize_film_design_with_structure(service, request)
    payload = json.loads(json.dumps(structure.film_structure_result_to_payload(request, result)))
    payload["state"] = "cancelled"
    payload["partial_result"]["candidate_verified"] = False
    payload["structure"]["topology_audit"]["cancel_snapshot"] = {
        "source": "worker_completed_reply", "replay_performed": False,
        "candidate_available": True, "training_complete": True,
    }
    def unexpected_tmm(*_args, **_kwargs):
        pytest.fail("cancel snapshot validation must use the saved complete optical evidence")
    monkeypatch.setattr(structure, "simulate_with_thickness_jacobian", unexpected_tmm)
    validate_completed_structure_cancel_result(payload)
    row = payload["candidate"]["raw_coverage"][0]
    row["wavelength_nm"], row["actual_values"] = row["wavelength_nm"][3:4], row["actual_values"][3:4]
    with pytest.raises(ValueError):
        validate_completed_structure_cancel_result(payload)


@pytest.mark.parametrize("execution_mode", ["serial", "deferred_batch"])
def test_meanonly_ordinary_display_receives_complete_raw_coverage(execution_mode):
    from coat_sim.core.film_candidate_display import CandidateDisplayObserver
    service, _design, base, _request = problem(pointwise=False, mean=True)
    packets: list[dict[str, Any]] = []
    observer = CandidateDisplayObserver(context_id="nested-mean-solver", objectives=base.objectives,
        control_provider=lambda: {"enabled": True, "objective_index": 0, "angle_deg": 0., "metric": "R"},
        publish_callback=packets.append)
    optimize_film_design(service, replace(base, solver="differential_evolution",
        execution_mode=execution_mode, max_evaluations=8, population_size=5, batch_size=3),
        candidate_observer=observer)
    assert not observer.last_error
    assert packets[-1]["best"]["condition_count"] == 2
    assert packets[-1]["best"]["availability"] == "available"
    assert len(packets[-1]["best"]["spectrum"]["actual"]) == 7
