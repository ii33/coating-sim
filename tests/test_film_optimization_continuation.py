"""Warm restarts use frozen geometry, never old scores as new solver work."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields, replace
import json

import pytest

from coat_sim.core import film_optimization_continuation as continuation
from coat_sim.core.film_auto_synthesis import (
    FilmSynthesisResult, _proxy, freeze_film_synthesis_request, synthesize_film_design,
)
from coat_sim.core.film_design import validate_design
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import (
    FilmOptimizationObjective, FilmOptimizationRequest, FilmOptimizationResult,
    FilmThicknessVariable, freeze_film_optimization_request,
)
from coat_sim.core.film_optimization_job import deserialize_request, serialize_request
from coat_sim.core.film_optimization_structure import (
    FilmStructureOptimizationRequest, evaluate_frozen_structure_candidate,
)
from coat_sim.core.film_synthesis_archive import SynthesisDesignArchive, physical_design_hash
from coat_sim.core.film_synthesis_io import (
    SCHEMA_VERSION, film_synthesis_request_from_payload, film_synthesis_request_to_payload,
    film_synthesis_result_to_payload,
)
from coat_sim.core.film_synthesis_jobs import is_synthesis_result
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.contract


def _layer(identifier, material="low", thickness=100.):
    return {"id": identifier, "material_id": material, "optical_set_id": "nk",
            "thickness": {"value": thickness, "unit": "nm"}, "note": "seed fixture"}


def _seed(environment, identifier, thickness, material="low"):
    seed = deepcopy(environment)
    seed["front_layers"] = [_layer(identifier, material, thickness)]
    return validate_design(seed)


def _criteria():
    return (FilmOptimizationObjective(520., 580., 4, angles_deg=(0., 25.),
        pointwise=FilmObjectiveConstraint(mode="range", target_value=None,
            lower_bound=.001, upper_bound=.8, strict_lower=True),
        mean=FilmObjectiveConstraint(mode="value", target_value=.12,
            lower_bound=None, upper_bound=None),
        weight=2.7, normalization_scale=.3),)


@pytest.fixture
def ordinary():
    service, synthesis = setup_problem()
    design = _seed(synthesis.environment, "selected", 100.)
    design["front_layers"].append(_layer("held", "high", 40.))
    request = freeze_film_optimization_request(service, FilmOptimizationRequest(
        validate_design(design), 7, (FilmThicknessVariable("front", "selected", 20., 200.),),
        _criteria(), max_iterations=8, max_evaluations=80, seed=42,
        multistart_count=3, population_size=7, validation_samples_multiplier=3,
        initial_step_fraction=.3, min_step_fraction=.0002,
        acceptable_normalized_error=.002, batch_size=9))
    candidate = deepcopy(request.design_snapshot)
    candidate["front_layers"][0]["thickness"]["value"] = 130.
    score = evaluate_frozen_structure_candidate(
        FilmStructureOptimizationRequest(request), candidate)["score"]
    result = FilmOptimizationResult("cancelled", request.design_revision, candidate,
        {"selected": 130.}, score["max_error"], score["weighted_max_error"],
        score["weighted_rms_error"], score["nominal_pass"], tuple(score["errors"]),
        2, 9, solver=request.solver, seed=request.seed, termination_reason="cancelled",
        failed_evaluations=1, validation_samples_multiplier=request.validation_samples_multiplier,
        solver_acceptable=score["solver_acceptable"], raw_coverage=tuple(score["coverage"]))
    return request, result


@pytest.fixture
def synthesis_payload():
    service, raw = setup_problem()
    environment = raw.environment
    original = (_seed(environment, "old-high", 120., "high"), deepcopy(environment))
    request = freeze_film_synthesis_request(service, replace(raw,
        objectives=_criteria(), search_strategy="hybrid", design_revision=11,
        max_cycles=8, max_evaluations=60, max_seconds=500.,
        seed_designs=original, candidate_materials=raw.candidate_materials[:1],
        minimum_nonzero_thickness_nm=2., max_layer_thickness_nm=180.,
        max_total_thickness_nm=300., max_layers=4, archive_capacity=3,
        randomize_template_search=True, structure_search_seed=31415,
        parallel_batch_size=2, max_working_set_bytes=None,
))
    candidate = _seed(environment, "best", 110.)
    scored = evaluate_frozen_structure_candidate(_proxy(request), candidate)
    archive = SynthesisDesignArchive(len(request.objectives), capacity=request.archive_capacity)
    for design in [candidate, _seed(environment, "archive-90", 90.),
                   _seed(environment, "archive-60", 60.)]:
        archive.add(evaluate_frozen_structure_candidate(_proxy(request), design),
                    provenance={"family": "fixture"})
    result = FilmSynthesisResult("cancelled", "cancelled", candidate,
        request.design_revision, False, scored["score"], None, False, 2, 9, 0, 1, (),
        "Cancelled after a complete candidate", request,
        performance={"search": {"design_archive": archive.snapshot()}},
        candidate_provenance={"selection": "completed_training_at_cancel",
            "training_complete": True, "frozen_at_cancel": True,
            "fully_scored_evaluations": 1, "validation_matches_candidate": False})
    payload = film_synthesis_result_to_payload("warm-restart-fixture", result)
    payload = json.loads(json.dumps(payload, allow_nan=False))
    assert is_synthesis_result(payload)
    return payload


def _no_physics(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("continuation construction performed optical simulation/replay/optimization")
    from coat_sim.core import (
        film_auto_synthesis, film_design_service, film_optimization, film_optimization_job,
        film_optimization_structure, film_synthesis_audit, film_synthesis_jobs,
    )
    for module, names in [
        (film_auto_synthesis, ("synthesize_film_design", "evaluate_frozen_structure_candidate")),
        (film_design_service, ("simulate_frozen",)),
        (film_optimization, ("optimize_film_design",)),
        (film_optimization_job, ("validate_checkpoint", "_replay_candidate_scores")),
        (film_optimization_structure, ("evaluate_frozen_structure_candidate",)),
        (film_synthesis_audit, ("audit_frozen_synthesis_candidate",)),
        (film_synthesis_jobs, ("evaluate_frozen_structure_candidate", "validate_checkpoint")),
    ]:
        for name in names:
            monkeypatch.setattr(module, name, forbidden)


def _ordinary_plan(pair, **budgets):
    request, result = pair
    return continuation.build_optimization_warm_restart(request, result, design_revision=18,
        max_iterations=budgets.get("max_iterations", 5),
        max_evaluations=budgets.get("max_evaluations", 21))


def _synthesis_plan(payload, **budgets):
    return continuation.build_synthesis_warm_restart(payload, design_revision=18,
        max_cycles=budgets.get("max_cycles", 5), max_evaluations=budgets.get("max_evaluations", 21),
        max_seconds=budgets.get("max_seconds", 75.))


def test_ordinary_preserves_all_frozen_criteria_settings_and_materials(ordinary, monkeypatch):
    request, result = ordinary
    before_request, before_result = deepcopy(request), deepcopy(result)
    _no_physics(monkeypatch)
    plan = _ordinary_plan(ordinary)
    assert request == before_request and result == before_result
    assert plan.request.design_revision == 18
    assert plan.request.design_snapshot == result.best_design
    assert plan.request.design_snapshot is not result.best_design
    for field in fields(request):
        if field.name not in {"design_snapshot", "frozen_material_snapshot", "design_revision",
                              "max_iterations", "max_evaluations"}:
            assert getattr(plan.request, field.name) == getattr(request, field.name), field.name
    assert plan.request.frozen_material_snapshot["materials"] == request.frozen_material_snapshot["materials"]
    for key in request.frozen_material_snapshot:
        if key not in {"design", "sha256"}:
            assert plan.request.frozen_material_snapshot[key] == request.frozen_material_snapshot[key]
    assert plan.request.frozen_material_snapshot["sha256"] != request.frozen_material_snapshot["sha256"]
    restored = deserialize_request(json.loads(json.dumps(serialize_request(plan.request))))
    assert restored == plan.request
    assert json.loads(json.dumps(plan.metadata)) == plan.metadata
    assert plan.metadata["original_request_sha256"] == continuation.canonical_request_digest(request)
    assert plan.metadata["candidate_physical_hash"] == physical_design_hash(result.best_design)
    assert plan.metadata["fresh_budget"] == {"max_iterations": 5, "max_evaluations": 21}
    assert plan.metadata["spent"] == {"iterations": 2, "evaluations": 9,
        "failed_evaluations": 1, "validation_evaluations": 0}
    assert plan.metadata["mode"] == "warm_restart" and not plan.metadata["solver_state_preserved"]
    assert "actual_values" not in json.dumps(plan.metadata)
    plan.request.design_snapshot["front_layers"][0]["thickness"]["value"] = 155.
    plan.request.frozen_material_snapshot["materials"][0]["name"] = "new copy"
    assert request == before_request and result == before_result


@pytest.mark.parametrize("change", ["state", "revision", "solver", "missing_score", "half_coverage",
    "nonfinite", "wrong_aggregate", "topology", "material", "fixed_thickness", "bounds",
    "missing_thickness", "zero_evaluations", "counter", "false_acceptance"])
def test_ordinary_rejects_incomplete_or_foreign_cancel_candidates(ordinary, change):
    request, result = deepcopy(ordinary)
    if change == "state":
        result = replace(result, state="completed")
    elif change == "revision":
        result = replace(result, design_revision=8)
    elif change == "solver":
        result = replace(result, seed=10)
    elif change == "missing_score":
        result = replace(result, per_condition_errors=())
    elif change == "half_coverage":
        result = replace(result, raw_coverage=result.raw_coverage[:1])
    elif change == "nonfinite":
        result = replace(result, max_normalized_error=float("nan"))
    elif change == "wrong_aggregate":
        result = replace(result, weighted_rms_error=3.)
    elif change == "topology":
        result.best_design["front_layers"][0]["id"] = "foreign"
    elif change == "material":
        result.best_design["front_layers"][0]["material_id"] = "glass"
    elif change == "fixed_thickness":
        result.best_design["front_layers"][1]["thickness"]["value"] += 1
    elif change == "bounds":
        result.best_design["front_layers"][0]["thickness"]["value"] = 210.
        result.best_thickness_nm["selected"] = 210.
    elif change == "missing_thickness":
        result.best_thickness_nm.clear()
    elif change == "zero_evaluations":
        result = replace(result, evaluations=0)
    elif change == "counter":
        result = replace(result, failed_evaluations=10)
    elif change == "false_acceptance":
        result = replace(result, targets_met=not result.targets_met)
    with pytest.raises(ValueError):
        _ordinary_plan((request, result))


@pytest.mark.parametrize("change", ["unfrozen", "digest", "invalid_bounds", "invalid_controls", "old_criteria"])
def test_ordinary_rejects_noncanonical_source_request(ordinary, change):
    request, result = deepcopy(ordinary)
    if change == "unfrozen":
        request = replace(request, frozen_material_snapshot=None)
    elif change == "digest":
        request.frozen_material_snapshot["sha256"] = "bad"
    elif change == "invalid_bounds":
        request = replace(request, variables=(replace(request.variables[0], lower_nm=200.),))
    elif change == "invalid_controls":
        request = replace(request, min_step_fraction=.8)
    elif change == "old_criteria":
        request = replace(request, objectives=(replace(request.objectives[0], wavelength_max_nm=520.),))
    with pytest.raises(ValueError):
        _ordinary_plan((request, result))


@pytest.mark.parametrize("budget", [0, -1, True, 1.5, None])
def test_ordinary_rejects_invalid_fresh_budget(ordinary, budget):
    with pytest.raises(ValueError):
        _ordinary_plan(ordinary, max_iterations=budget)
    with pytest.raises(ValueError):
        _ordinary_plan(ordinary, max_evaluations=budget)


def test_synthesis_keeps_best_archive_and_original_seeds_without_score_reuse(synthesis_payload, monkeypatch):
    payload = synthesis_payload
    before = deepcopy(payload)
    _no_physics(monkeypatch)
    plan = _synthesis_plan(payload)
    assert payload == before
    _, original, _ = film_synthesis_request_from_payload({
        "schema_version": SCHEMA_VERSION, "kind": "coat_sim.film_synthesis_request",
        "request_id": payload["request_id"], "request": payload["result"]["frozen_request"],
        "physical_structures": []})
    for field in fields(original):
        if field.name not in {"seed_designs", "design_revision", "max_cycles", "max_evaluations",
                              "max_seconds", "warm_start_seed_index"}:
            assert getattr(plan.request, field.name) == getattr(original, field.name), field.name
    assert plan.request.warm_start_seed_index == 0
    assert plan.request.seed_designs[0] == payload["result"]["candidate_design"]
    hashes = [physical_design_hash(seed) for seed in plan.request.seed_designs]
    assert len(hashes) == len(set(hashes)) == 5
    assert [seed["front_layers"][0]["id"] if seed["front_layers"] else "bare"
            for seed in plan.request.seed_designs][-2:] == ["old-high", "bare"]
    archive = payload["result"]["performance"]["search"]["design_archive"]["entries"]
    best = hashes[0]
    expected = sorted((entry for entry in archive if entry["physical_hash"] != best),
        key=lambda entry: (entry["max_error"], entry["weighted_rms_error"],
            entry["layer_count"], entry["total_thickness_nm"], entry["physical_hash"]))
    assert hashes[1:3] == [entry["physical_hash"] for entry in expected]
    assert plan.metadata["seeds"]["archive_added"] == 2
    assert plan.metadata["fresh_budget"] == {"max_cycles": 5, "max_evaluations": 21, "max_seconds": 75.}
    assert plan.metadata["spent"]["evaluations"] == 9
    assert plan.metadata["saved_scores_reused"] is False
    assert "actual_values" not in json.dumps(asdict(plan.request))
    envelope = film_synthesis_request_to_payload("new-job", plan.request)
    assert envelope["schema_version"] == 6
    _, restored, _ = film_synthesis_request_from_payload(json.loads(json.dumps(envelope)))
    assert restored == plan.request
    assert _synthesis_plan(json.loads(json.dumps(payload))).request == plan.request
    assert json.loads(json.dumps(plan.metadata)) == plan.metadata
    plan.request.seed_designs[0]["front_layers"][0]["thickness"]["value"] += 5
    assert payload == before


def test_synthesis_deduplicates_physically_equal_original_seeds(synthesis_payload):
    payload = synthesis_payload
    original = payload["result"]["frozen_request"]
    duplicate = deepcopy(original["seed_designs"][0])
    duplicate["front_layers"][0]["id"] = "same-geometry-different-editor-id"
    original["seed_designs"].append(duplicate)
    assert is_synthesis_result(payload)
    plan = _synthesis_plan(payload)
    assert len(plan.request.seed_designs) == 5
    assert plan.metadata["seeds"]["original_count"] == 3


def test_synthesis_bound_protects_all_original_seed_branches(synthesis_payload):
    payload = synthesis_payload
    payload["result"]["frozen_request"]["archive_capacity"] = 2
    archive = payload["result"]["performance"]["search"]["design_archive"]
    archive["capacity"] = 2
    archive["entries"].pop()
    archive["retained"] -= 1
    archive["total_pruned"] += 1
    plan = _synthesis_plan(payload)
    assert plan.metadata["seeds"]["archive_added"] == 1
    assert len(plan.request.seed_designs) == 4
    assert plan.request.seed_designs[-2:] == tuple(payload["result"]["frozen_request"]["seed_designs"])


@pytest.mark.parametrize("mutation", ["schema", "state", "missing_candidate", "missing_score",
    "half_grid", "revision", "digest", "archive_hash", "archive_outside_pool",
    "archive_bounds", "archive_count", "archive_nonfinite", "archive_duplicate",
    "archive_missing_design", "archive_bad_audit", "bad_original_seed"])
def test_synthesis_rejects_incomplete_foreign_and_corrupt_results(synthesis_payload, mutation):
    payload = deepcopy(synthesis_payload)
    result = payload["result"]
    archive = result["performance"]["search"]["design_archive"]
    entry = archive["entries"][-1]
    if mutation == "schema":
        payload["schema_version"] -= 1
    elif mutation == "state":
        result["state"] = "failed"
    elif mutation == "missing_candidate":
        result["candidate_design"] = None
    elif mutation == "missing_score":
        result["training_score"] = None
    elif mutation == "half_grid":
        result["training_score"]["coverage"].pop()
    elif mutation == "revision":
        result["design_revision"] += 1
    elif mutation == "digest":
        result["frozen_request"]["frozen_material_snapshot"]["sha256"] = "bad"
    elif mutation == "archive_hash":
        entry["physical_hash"] = "f" * 64
    elif mutation == "archive_outside_pool":
        entry["design"]["front_layers"][0]["material_id"] = "glass"
        entry["physical_hash"] = physical_design_hash(entry["design"])
    elif mutation == "archive_bounds":
        entry["design"]["front_layers"][0]["thickness"]["value"] = 190.
        entry["physical_hash"] = physical_design_hash(entry["design"])
        entry["total_thickness_nm"] = 190.
    elif mutation == "archive_count":
        archive["retained"] = 42
    elif mutation == "archive_nonfinite":
        entry["max_error"] = float("inf")
    elif mutation == "archive_duplicate":
        archive["entries"][-1] = deepcopy(archive["entries"][0])
    elif mutation == "archive_missing_design":
        entry.pop("design")
    elif mutation == "archive_bad_audit":
        entry["audit_status"] = "complete"
    elif mutation == "bad_original_seed":
        result["frozen_request"]["seed_designs"][0]["front_layers"][0]["thickness"]["value"] = 190.
    with pytest.raises(ValueError):
        _synthesis_plan(payload)


@pytest.mark.parametrize("budgets", [{"max_cycles": 0}, {"max_cycles": True},
    {"max_evaluations": 1}, {"max_evaluations": False}, {"max_evaluations": 2.5},
    {"max_seconds": 0.}, {"max_seconds": float("nan")}, {"max_seconds": True}])
def test_synthesis_rejects_invalid_fresh_budget(synthesis_payload, budgets):
    with pytest.raises(ValueError):
        _synthesis_plan(synthesis_payload, **budgets)


def test_synthesis_unlimited_fresh_budget_is_explicit_not_old_spent(synthesis_payload):
    plan = _synthesis_plan(synthesis_payload, max_cycles=None, max_evaluations=None, max_seconds=None)
    assert plan.metadata["fresh_budget"] == {
        "max_cycles": None, "max_evaluations": None, "max_seconds": None}
    assert plan.request.max_cycles is None and plan.request.max_evaluations is None
    assert plan.request.max_seconds is None and plan.metadata["spent"]["evaluations"] == 9


def test_minimum_new_synthesis_budget_evaluates_retained_candidate_first(synthesis_payload):
    plan = _synthesis_plan(synthesis_payload, max_evaluations=2, max_cycles=1, max_seconds=None)
    result = synthesize_film_design(None, plan.request)
    assert result.candidate_design is not None
    assert physical_design_hash(result.candidate_design) == plan.metadata["candidate_physical_hash"]
    assert result.training_score is not None and result.evaluations <= 2
    assert result.training_score is not synthesis_payload["result"]["training_score"]


def test_ordinary_cancel_during_holdout_keeps_training_and_validation_counters_separate(ordinary):
    request, result = ordinary
    # The ordinary worker counts its holdout separately from training attempts.
    result = replace(result, evaluations=1, failed_evaluations=0,
                     validation_evaluations=1, validation_message="holdout interrupted")
    plan = _ordinary_plan((request, result))
    assert plan.metadata["spent"]["evaluations"] == 1
    assert plan.metadata["spent"]["validation_evaluations"] == 1
    assert plan.metadata["fresh_budget"]["max_evaluations"] == 21


def test_ordinary_saved_complete_holdout_is_checked_without_replay(ordinary, monkeypatch):
    from coat_sim.core.film_optimization_job import _validation_objectives
    request, result = ordinary
    dense = evaluate_frozen_structure_candidate(FilmStructureOptimizationRequest(
        replace(request, objectives=_validation_objectives(request))), result.best_design)["score"]
    result = replace(result, evaluations=1, failed_evaluations=0,
        validation_evaluations=1, validation_complete=True,
        validation_max_normalized_error=dense["max_error"],
        validation_weighted_rms_error=dense["weighted_rms_error"],
        validation_targets_met=dense["nominal_pass"],
        validation_solver_acceptable=dense["solver_acceptable"],
        validation_per_condition_errors=tuple(dense["errors"]),
        validation_raw_coverage=tuple(dense["coverage"]))
    _no_physics(monkeypatch)
    plan = _ordinary_plan((request, result))
    assert not plan.metadata["independent_audit_performed"]
    with pytest.raises(ValueError, match="coverage"):
        _ordinary_plan((request, replace(result, validation_raw_coverage=())))


@pytest.mark.parametrize("unit,value", [("um", .1), ("qw", 1.)])
def test_ordinary_restarts_physical_nm_candidate_not_original_unit(ordinary, unit, value):
    from coat_sim.core.film_optimization_job import _snapshot_for_candidate
    request, result = deepcopy(ordinary)
    source = deepcopy(request.design_snapshot)
    source["front_layers"][0]["thickness"] = {"value": value, "unit": unit}
    request = replace(request, design_snapshot=source,
        variables=(replace(request.variables[0], upper_nm=None),),
        frozen_material_snapshot=_snapshot_for_candidate(request, source))
    plan = _ordinary_plan((request, result))
    assert plan.request.design_snapshot["front_layers"][0]["thickness"] == {
        "value": 130., "unit": "nm"}
    assert plan.request.variables[0].upper_nm is None
    assert request.design_snapshot["front_layers"][0]["thickness"]["unit"] == unit


def test_synthesis_legacy_real_cancel_keeps_geometry_and_new_budget(monkeypatch):
    from tests.test_film_synthesis_cancel_jobs import _cancelled_payload
    payload = _cancelled_payload()
    assert payload["result"]["frozen_request"]["search_strategy"] == "legacy"
    _no_physics(monkeypatch)
    plan = _synthesis_plan(payload)
    assert plan.request.search_strategy == "legacy"
    assert plan.request.warm_start_seed_index == 0
    assert plan.request.seed_designs[0] == payload["result"]["candidate_design"]
    assert plan.metadata["spent"]["evaluations"] == payload["result"]["evaluations"]


def test_synthesis_without_exported_archive_still_preserves_original_seeds(synthesis_payload):
    payload = synthesis_payload
    payload["result"]["performance"]["search"].pop("design_archive")
    plan = _synthesis_plan(payload)
    assert plan.metadata["seeds"]["archive_added"] == 0
    assert len(plan.request.seed_designs) == 3
    assert plan.request.seed_designs[1:] == tuple(payload["result"]["frozen_request"]["seed_designs"])


def test_synthesis_checks_every_exported_seed_before_capacity_filter(synthesis_payload):
    payload = synthesis_payload
    archive = payload["result"]["performance"]["search"]["design_archive"]
    # None of these three archive designs duplicates the separately retained
    # best, so one would be dropped by the fresh geometry capacity of three.
    first = archive["entries"][0]
    first["design"]["front_layers"][0]["thickness"]["value"] += 1.
    first["total_thickness_nm"] += 1.
    first["physical_hash"] = physical_design_hash(first["design"])
    worst = archive["entries"][-1]
    worst["design"]["front_layers"][0]["material_id"] = "glass"
    worst["physical_hash"] = physical_design_hash(worst["design"])
    worst["max_error"] = 1.e10
    with pytest.raises(ValueError, match="physical inputs"):
        _synthesis_plan(payload)


@pytest.mark.parametrize("revision", [-1, True, 1.5, None])
def test_both_builders_require_explicit_nonnegative_integer_revision(ordinary, synthesis_payload, revision):
    with pytest.raises(ValueError):
        continuation.build_optimization_warm_restart(*ordinary, design_revision=revision,
            max_iterations=2, max_evaluations=2)
    with pytest.raises(ValueError):
        continuation.build_synthesis_warm_restart(synthesis_payload, design_revision=revision,
            max_cycles=2, max_evaluations=2, max_seconds=None)


def test_synthesis_rejects_numerically_equal_noninteger_schema(synthesis_payload):
    synthesis_payload["schema_version"] = float(SCHEMA_VERSION)
    with pytest.raises(ValueError, match="schema"):
        _synthesis_plan(synthesis_payload)
