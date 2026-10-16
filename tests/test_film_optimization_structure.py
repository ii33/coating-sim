from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from threading import Event

import numpy as np
import pytest

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from coat_sim.core.film_optimization_structure import (
    FilmStructureOptimizationRequest,
    _insert_at_site,
    _project_group_values,
    _site_records,
    _validation_objectives,
    evaluate_frozen_structure_candidate,
    film_structure_result_to_payload,
    freeze_film_structure_optimization_request,
    optimize_film_design_with_structure,
    validate_film_structure_candidate_topology,
    validate_film_structure_payload,
)
from coat_sim.core import film_optimization_structure as structure
from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_design_service import FilmDesignService


def _optical(identifier: str, n: float) -> dict:
    return {
        "id": identifier, "name": identifier, "kind": "tabulated", "notes": "test",
        "source": "synthetic test data", "wavelength_unit": "nm",
        "wavelength_min": 450.0, "wavelength_max": 700.0,
        "wavelengths": [450.0, 700.0], "n": [n, n], "k": [0.0, 0.0],
    }


class _MemoryMaterials:
    def __init__(self, records):
        self._by_id = {row["id"]: deepcopy(row) for row in records}

    def _records(self, material_ids=None):
        selected = set(material_ids) if material_ids is not None else set(self._by_id)
        return [deepcopy(row) for key, row in self._by_id.items() if key in selected]

    def get(self, identifier):
        return deepcopy(self._by_id[identifier])


class _MemoryService:
    def __init__(self, records):
        self.materials = _MemoryMaterials(records)

    validate = staticmethod(validate_design)
    freeze_design = FilmDesignService.freeze_design


def _service(tmp_path=None):
    records = []
    for identifier, category, optical in (
        ("struct_glass", "substrate", _optical("glass15", 1.5)),
        ("struct_film", "film", _optical("film12", 1.2)),
        ("struct_high", "film", _optical("high20", 2.0)),
    ):
        roles = ["substrate"] if category == "substrate" else ["film"]
        records.append({"id": identifier, "name": identifier, "enabled": True,
                        "design_roles": roles, "design_ready": True,
                        "notes": "memory-only fixture", "optical_sets": [optical]})
    service = _MemoryService(records)
    design = new_design()
    design["substrate"].update(material_id="struct_glass", optical_set_id="glass15")
    design["front_layers"] = [{
        "id": "stable-host", "material_id": "struct_film", "optical_set_id": "film12",
        "thickness": {"value": 100.0, "unit": "nm"}, "note": "host",
    }]
    return service, design


def _request(design, *, mode="combined", variables=True, max_evaluations=40, max_iterations=1,
             **options):
    base = FilmOptimizationRequest(
        design_snapshot=deepcopy(design), design_revision=7,
        variables=((FilmThicknessVariable("front", "stable-host", 80.0, 140.0),) if variables else ()),
        objectives=(FilmOptimizationObjective(
            wavelength_min_nm=550.0, wavelength_max_nm=550.0, wavelength_samples=1,
            angles_deg=(0.0,), polarization="unpolarized", metric="R", mode="value",
            target_value=0.1,
        ),),
        max_evaluations=max_evaluations, max_iterations=max_iterations,
    )
    structure_options = {
        "mode": mode, "max_total_thickness_nm": 500.0,
        "min_thickness_nm": 1.0, "max_thickness_nm": 180.0,
        "max_total_layers": 8, "candidate_budget": 6, "position_samples": 3,
        "needle_thickness_samples": 2, "growth_steps_nm": (2.0, 20.0),
        "growth_phase_fractions": (0.25, 0.5, 1.0), "seed": 19,
    }
    structure_options.update(options)
    return FilmStructureOptimizationRequest(
        base_request=base,
        candidate_materials=(FilmNeedleMaterialReference("struct_high", "high20"),),
        **structure_options,
    )


def test_zero_probe_derivative_matches_one_sided_finite_difference_and_split_conserves(tmp_path):
    service, design = _service(tmp_path)
    frozen = freeze_film_structure_optimization_request(service, _request(design, mode="needle_only"))
    site = next(row for row in _site_records(design, frozen.base_request.frozen_material_snapshot, 3)
                if row["kind"] == "interior")
    reference = frozen.candidate_materials[0]
    needle_id, split_id = "temporary-zero-needle", "temporary-right-host"
    zero = _insert_at_site(design, site, reference, 0.0, needle_id, split_id, zero=True)
    probed = evaluate_frozen_structure_candidate(
        frozen, zero, variable_ids=(needle_id,), with_jacobian=True,
        zero_thickness_layer_ids=(needle_id,),
    )
    step = 1e-4
    finite = _insert_at_site(design, site, reference, step, needle_id, split_id, zero=False)
    perturbed = evaluate_frozen_structure_candidate(
        frozen, finite, variable_ids=(needle_id,), with_jacobian=True)
    derivative = (perturbed["smooth_loss"] - probed["smooth_loss"]) / step
    assert probed["gradient_by_layer_id"][needle_id] == pytest.approx(derivative, rel=2e-3, abs=1e-8)
    finite_nm = _insert_at_site(design, site, reference, 20.0, needle_id, split_id, zero=False)
    ids = [row["id"] for row in finite_nm["front_layers"]]
    assert ids == ["stable-host", needle_id, split_id]
    assert finite_nm["front_layers"][0]["thickness"]["value"] + finite_nm["front_layers"][2]["thickness"]["value"] == pytest.approx(100.0)
    host_metadata = {key: value for key, value in design["front_layers"][0].items()
                     if key not in {"id", "thickness"}}
    for split_row in (finite_nm["front_layers"][0], finite_nm["front_layers"][2]):
        assert {key: value for key, value in split_row.items()
                if key not in {"id", "thickness"}} == host_metadata
    assert finite_nm["front_layers"][0]["id"] == "stable-host"
    assert finite_nm["front_layers"][2]["id"] == split_id
    expression_design = deepcopy(design)
    expression_design["expression"] = {"text": "stable-host", "in_sync": True, "note": "keep"}
    detached = _insert_at_site(
        expression_design, site, reference, 20.0, needle_id, split_id, zero=False)
    assert detached["expression"] == {"text": "stable-host", "in_sync": False, "note": "keep"}
    for key, value in expression_design.items():
        if key not in {"front_layers", "expression"}:
            assert detached[key] == value


def test_group_projection_respects_box_and_total_bound():
    projected = _project_group_values(
        np.array([10.0, 2.0]), np.array([0.0, 0.0]), np.array([3.0, 3.0]), 3.0)
    assert projected == pytest.approx([3.0, 0.0])


def test_validation_grid_chunks_global_10003_point_holdout_without_training_cap_change(tmp_path, monkeypatch):
    service, design = _service(tmp_path)
    request = _request(design, mode="needle_only", variables=False)
    objective = replace(request.base_request.objectives[0],
                        wavelength_min_nm=450.0, wavelength_max_nm=700.0,
                        wavelength_samples=5002)
    request = replace(request, base_request=replace(
        request.base_request, objectives=(objective,), validation_samples_multiplier=2))
    frozen = freeze_film_structure_optimization_request(service, request)
    validation = _validation_objectives(frozen)
    assert frozen.base_request.objectives[0].wavelength_samples == 5002
    assert validation[0].wavelength_samples == 10003
    assert sum(row.wavelength_samples * len(row.angles_deg)
               for row in frozen.base_request.objectives) <= 20000
    settings_seen = []

    def observe_chunks(candidate, frozen_payload, variables, settings, **kwargs):
        settings_seen.append(dict(settings))
        waves = np.linspace(settings["wavelength_min_nm"], settings["wavelength_max_nm"],
                            settings["samples"])
        return {"wavelength_nm": waves, "R": np.zeros(waves.size),
                "jacobian_R": np.zeros((waves.size, len(variables)))}

    monkeypatch.setattr(structure, "simulate_with_thickness_jacobian", observe_chunks)
    evaluated = evaluate_frozen_structure_candidate(
        frozen, frozen.base_request.design_snapshot,
        variable_ids=("stable-host",), with_jacobian=True,
        derived_validation_grid=True)
    assert [row["samples"] for row in settings_seen] == [10001, 2]
    expected = np.linspace(450.0, 700.0, 10003)
    actual = np.asarray(evaluated["responses"][0]["wavelength_nm"])
    assert actual.size == 10003
    assert np.all(np.diff(actual) > 0.0)
    assert actual == pytest.approx(expected, abs=1e-12)
    assert evaluated["jacobian_calls"] == 2

    former_cap_training_grid = replace(frozen, base_request=replace(
        frozen.base_request,
        objectives=(replace(frozen.base_request.objectives[0], wavelength_samples=10003),)))
    settings_seen.clear()
    accepted = evaluate_frozen_structure_candidate(
        former_cap_training_grid, frozen.base_request.design_snapshot,
        with_jacobian=False, derived_validation_grid=False)
    assert accepted["responses"][0]["wavelength_nm"][-1] == 700.0
    assert [row["samples"] for row in settings_seen] == [10001, 2]


def test_structure_eval_counts_only_nonempty_analytic_jacobian_calls(tmp_path, monkeypatch):
    service, design = _service(tmp_path)
    request = _request(design, mode="needle_only", variables=False)
    objective = replace(request.base_request.objectives[0], angles_deg=(0.0, 30.0))
    request = replace(request, base_request=replace(request.base_request, objectives=(objective,)))
    frozen = freeze_film_structure_optimization_request(service, request)
    value_only = evaluate_frozen_structure_candidate(frozen, design, with_jacobian=False)
    empty_jac = evaluate_frozen_structure_candidate(frozen, design, with_jacobian=True)
    nonempty_jac = evaluate_frozen_structure_candidate(
        frozen, design, variable_ids=("stable-host",), with_jacobian=True)
    assert value_only["jacobian_calls"] == 0
    assert len(value_only["responses"]) == 2
    assert [row["angle_deg"] for row in value_only["responses"]] == [0.0, 30.0]
    assert empty_jac["jacobian_calls"] == 0
    assert nonempty_jac["jacobian_calls"] == 2

    single_angle = replace(frozen, base_request=replace(
        frozen.base_request,
        objectives=(replace(frozen.base_request.objectives[0], angles_deg=(30.0,)),)))
    single = evaluate_frozen_structure_candidate(
        single_angle, design, with_jacobian=False)
    assert len(single["responses"]) == 1
    assert single["responses"][0]["angle_deg"] == 30.0

    def cancel_started_helper(*_args, **_kwargs):
        raise structure._StructureCancelled()

    monkeypatch.setattr(structure, "simulate_with_thickness_jacobian", cancel_started_helper)
    with pytest.raises(structure._StructureCancelled) as cancelled:
        evaluate_frozen_structure_candidate(
            frozen, design, variable_ids=("stable-host",), with_jacobian=True)
    assert cancelled.value.jacobian_calls == 1


def test_large_layer_stack_uses_helper_safe_global_chunks(tmp_path, monkeypatch):
    service, design = _service(tmp_path)
    request = _request(design, mode="needle_only", variables=False, max_total_layers=128)
    objective = replace(request.base_request.objectives[0],
                        wavelength_min_nm=450.0, wavelength_max_nm=700.0,
                        wavelength_samples=5002)
    request = replace(request, base_request=replace(
        request.base_request, objectives=(objective,), validation_samples_multiplier=2))
    frozen = freeze_film_structure_optimization_request(service, request)
    multi_layer_design = deepcopy(design)
    host = deepcopy(design["front_layers"][0])
    for index in range(1, 128):
        layer = deepcopy(host)
        layer["id"] = f"host-{index}"
        multi_layer_design["front_layers"].append(layer)
    settings_seen = []

    def synthetic_response(candidate, frozen_payload, variables, settings, **kwargs):
        settings_seen.append(dict(settings))
        waves = np.linspace(settings["wavelength_min_nm"], settings["wavelength_max_nm"],
                            settings["samples"])
        return {
            "wavelength_nm": waves,
            "R": np.full(waves.size, 0.1),
            "jacobian_R": np.zeros((waves.size, len(variables))),
        }

    monkeypatch.setattr(structure, "simulate_with_thickness_jacobian", synthetic_response)
    evaluated = evaluate_frozen_structure_candidate(
        frozen, multi_layer_design, variable_ids=("stable-host",),
        with_jacobian=True, derived_validation_grid=True)
    assert [row["samples"] for row in settings_seen] == [7633, 2370]
    expected = np.linspace(450.0, 700.0, 10003)
    actual = np.asarray(evaluated["responses"][0]["wavelength_nm"])
    assert actual.size == 10003
    assert np.all(np.diff(actual) > 0.0)
    assert actual == pytest.approx(expected, abs=1e-12)
    assert evaluated["jacobian_calls"] == 2

    settings_seen.clear()
    tail_objective = replace(objective, wavelength_samples=7634)
    tail_request = replace(request, base_request=replace(
        request.base_request, objectives=(tail_objective,), validation_samples_multiplier=2))
    tail_frozen = freeze_film_structure_optimization_request(service, tail_request)
    tail_evaluated = evaluate_frozen_structure_candidate(
        tail_frozen, multi_layer_design, variable_ids=("stable-host",),
        with_jacobian=True, derived_validation_grid=True)
    assert [row["samples"] for row in settings_seen] == [7633, 7633, 1]
    assert settings_seen[-1]["wavelength_min_nm"] == settings_seen[-1]["wavelength_max_nm"] == 700.0
    tail_waves = np.asarray(tail_evaluated["responses"][0]["wavelength_nm"])
    assert tail_waves.size == 15267
    assert np.all(np.diff(tail_waves) > 0.0)
    assert tail_waves == pytest.approx(np.linspace(450.0, 700.0, 15267), abs=1e-12)
    assert tail_evaluated["jacobian_calls"] == 3


def test_cancel_after_training_score_preserves_unvalidated_best_and_pending_state(tmp_path):
    service, design = _service(tmp_path)
    request = _request(design, mode="needle_only", variables=False)
    cancellation = Event()

    def cancel_after_base_score(progress):
        if progress.message == "scored the frozen base design":
            cancellation.set()

    result = optimize_film_design_with_structure(
        service, request, cancel_event=cancellation,
        progress_callback=cancel_after_base_score)
    assert result.state == "cancelled"
    assert result.base_result.best_design["front_layers"] == design["front_layers"]
    assert result.base_result.per_condition_errors
    assert result.base_result.validation_complete is False


def test_holdout_failure_marks_failed_and_retains_training_incumbent(tmp_path, monkeypatch):
    service, design = _service(tmp_path)
    request = _request(design, mode="needle_only", variables=False, candidate_budget=1)
    original = structure.evaluate_frozen_structure_candidate

    def fail_holdout(frozen_request, candidate_design, **kwargs):
        if not kwargs.get("with_jacobian", False):
            raise RuntimeError("synthetic holdout failure")
        return original(frozen_request, candidate_design, **kwargs)

    monkeypatch.setattr(structure, "evaluate_frozen_structure_candidate", fail_holdout)
    result = optimize_film_design_with_structure(service, request)
    assert result.state == "failed"
    assert result.base_result.best_design["front_layers"] == design["front_layers"]
    assert result.base_result.per_condition_errors
    assert result.base_result.validation_complete is False


@pytest.mark.parametrize("outcome", ["failure", "cancel"])
def test_post_incumbent_polish_holdout_failure_or_cancel_preserves_verified_incumbent(
        tmp_path, monkeypatch, outcome):
    service, design = _service(tmp_path)
    request = _request(design, mode="growth_only", max_evaluations=32, max_iterations=4)
    original_objective = request.base_request.objectives[0]
    request = replace(request, base_request=replace(
        request.base_request,
        variables=(FilmThicknessVariable("front", "stable-host", 99.0, 101.0),),
        initial_step_fraction=0.5,
        max_iterations=4,
    ))
    frozen = freeze_film_structure_optimization_request(service, request)
    original = structure.evaluate_frozen_structure_candidate
    base_response = original(frozen, design, with_jacobian=False)
    base_actual = float(base_response["responses"][0]["actual_values"][0])
    endpoint_values = []
    for thickness in (99.0, 101.0):
        endpoint = deepcopy(design)
        endpoint["front_layers"][0]["thickness"]["value"] = thickness
        response = original(frozen, endpoint, with_jacobian=False)
        endpoint_values.append((
            abs(float(response["responses"][0]["actual_values"][0]) - base_actual),
            thickness, float(response["responses"][0]["actual_values"][0]),
        ))
    difference, target_thickness, target_value = max(endpoint_values)
    assert difference > 1e-10
    target_objective = replace(original_objective, target_value=target_value)
    request = replace(request, base_request=replace(
        request.base_request, objectives=(target_objective,)))
    cancellation = Event()
    validation_designs = []
    improved_training_designs = []

    def control_candidate_holdout(frozen_request, candidate_design, **kwargs):
        thickness = float(candidate_design["front_layers"][0]["thickness"]["value"])
        is_holdout = kwargs.get("derived_validation_grid") is True
        if is_holdout:
            validation_designs.append(thickness)
            if abs(thickness - target_thickness) < 1e-8:
                if outcome == "failure":
                    raise RuntimeError("synthetic later holdout failure")
                cancellation.set()
                raise structure._StructureCancelled()
        elif kwargs.get("with_jacobian") is True and abs(thickness - target_thickness) < 1e-8:
            improved_training_designs.append(thickness)
        return original(frozen_request, candidate_design, **kwargs)

    monkeypatch.setattr(structure, "evaluate_frozen_structure_candidate", control_candidate_holdout)
    result = optimize_film_design_with_structure(
        service, request, cancel_event=cancellation)

    assert validation_designs == [100.0, target_thickness]
    assert improved_training_designs
    assert result.state == ("failed" if outcome == "failure" else "cancelled")
    assert result.base_result.best_design["front_layers"] == design["front_layers"]
    assert result.base_result.per_condition_errors
    assert result.base_result.validation_complete is True
    assert result.base_result.validation_per_condition_errors
    pending = [event for event in result.trace if event.stage == "validation" and
               event.candidate_kind == "baseline" and event.candidate_id == "baseline" and
               "holdout remains pending" in event.message]
    assert len(pending) == 1
    assert pending[0].accepted is False and pending[0].verified is False


def test_unfeasible_needle_candidates_leave_baseline_unchanged(tmp_path):
    service, design = _service(tmp_path)
    request = _request(design, mode="needle_only", variables=False, candidate_budget=3,
                       max_total_thickness_nm=100.0)
    request = replace(request, candidate_materials=(FilmNeedleMaterialReference("struct_film", "film12"),))
    result = optimize_film_design_with_structure(service, request)
    assert result.base_result.best_design["front_layers"] == design["front_layers"]
    assert result.added_needle_layer_ids == ()


def test_growth_scoring_never_violates_selected_layer_total_bounds(tmp_path, monkeypatch):
    service, design = _service(tmp_path)
    design["front_layers"][0]["thickness"]["value"] = 130.0
    request = _request(
        design, mode="combined", max_evaluations=28, growth_steps_nm=(20.0,),
        growth_phase_fractions=(1.0,), growth_cycles=1)
    original = structure.evaluate_frozen_structure_candidate
    scored_designs = []
    audited_selected_groups = 0

    def audit_before_score(frozen_request, candidate_design, **kwargs):
        nonlocal audited_selected_groups
        zero_ids = set(kwargs.get("zero_thickness_layer_ids", ()))
        front_rows = candidate_design["front_layers"]
        for row in front_rows:
            if row["id"] in zero_ids:
                continue
            thickness = row["thickness"]["value"]
            assert request.min_thickness_nm <= thickness <= request.max_thickness_nm
        split_rows = [row for row in front_rows if row["id"].startswith("structure-split-")]
        stable_host = next((row for row in front_rows if row["id"] == "stable-host"), None)
        selected_rows = [row for row in (stable_host, *split_rows) if row is not None]
        if split_rows and stable_host is not None and len(selected_rows) == 2:
            selected_total = sum(row["thickness"]["value"] for row in selected_rows)
            assert 80.0 <= selected_total <= 140.0
            audited_selected_groups += 1
        elif stable_host is not None:
            assert 80.0 <= stable_host["thickness"]["value"] <= 140.0
            audited_selected_groups += 1
        scored_designs.append(candidate_design)
        return original(frozen_request, candidate_design, **kwargs)

    monkeypatch.setattr(structure, "evaluate_frozen_structure_candidate", audit_before_score)
    result = optimize_film_design_with_structure(service, request)
    assert scored_designs
    assert audited_selected_groups
    assert result.counters.objective_evaluations <= request.base_request.max_evaluations
    assert any(event.stage == "growth" and event.candidate_kind == "growth"
               for event in result.trace)
    assert any(
        event.stage == "growth" and event.candidate_kind == "growth" and
        "before scoring" in event.message
        for event in result.trace
    )


def test_structure_optimizer_runs_bounded_three_stage_trace_and_replay_rejects_tampering(tmp_path):
    service, design = _service(tmp_path)
    request = _request(design, max_iterations=8)
    result = optimize_film_design_with_structure(service, request)
    initial_sensitivity = next(event for event in result.trace
                               if event.stage == "needle_initial_sensitivity")
    initial_needle = next(event for event in result.trace
                           if event.stage == "needle_initial_proposal" and
                           event.candidate_kind == "needle" and
                           event.thickness_nm is not None and event.thickness_nm > 0)
    growth_trial = next(event for event in result.trace
                        if event.stage == "growth_escape" and
                        event.candidate_kind == "growth_trial" and
                        event.thickness_nm is not None and event.thickness_nm > 0)
    growth_branch = next(event for event in result.trace
                         if event.stage == "growth_escape" and
                         event.candidate_kind == "growth_branch" and event.selected)
    after_growth = next(event for event in result.trace
                        if event.stage == "needle_after_growth_sensitivity")
    baseline_polish = next(event for event in result.trace if event.stage == "baseline_polish")
    assert baseline_polish.sequence < initial_sensitivity.sequence < initial_needle.sequence
    assert initial_needle.sequence < growth_trial.sequence < growth_branch.sequence < after_growth.sequence
    assert growth_trial.parent_candidate_id == "baseline"
    assert growth_branch.parent_candidate_id == "baseline"
    assert result.counters.candidate_count <= request.candidate_budget
    assert result.counters.objective_evaluations <= request.base_request.max_evaluations
    assert result.counters.local_iterations <= request.base_request.max_iterations
    payload = film_structure_result_to_payload(request, result)
    canonical = validate_film_structure_payload(service, payload)
    assert canonical["partial_result"]["candidate_verified"] is True
    assert canonical["partial_result"]["validation_complete"] == result.base_result.validation_complete
    corrupted = deepcopy(payload)
    corrupted["candidate"]["per_condition_errors"][0]["actual"] += 0.01
    with pytest.raises(ValueError, match="per-condition training errors"):
        validate_film_structure_payload(service, corrupted)


def test_topology_checker_rejects_changed_unselected_rows_and_hard_limits(tmp_path):
    service, design = _service(tmp_path)
    frozen = freeze_film_structure_optimization_request(
        service, _request(design, mode="needle_only", variables=False))
    with pytest.raises(ValueError, match="unselected base layer"):
        changed = deepcopy(design)
        changed["front_layers"][0]["thickness"]["value"] = 110.0
        validate_film_structure_candidate_topology(frozen, changed)
    with pytest.raises(ValueError, match="hard limit"):
        over = deepcopy(design)
        over["front_layers"][0]["thickness"]["value"] = 181.0
        validate_film_structure_candidate_topology(frozen, over)


def test_request_rejects_noninteger_added_layer_budget_and_budget_is_bounded(tmp_path):
    service, design = _service(tmp_path)
    invalid = _request(design, max_added_layers=True)
    with pytest.raises(ValueError):
        freeze_film_structure_optimization_request(service, invalid)
    tiny = _request(design, mode="needle_only", max_evaluations=2)
    result = optimize_film_design_with_structure(service, tiny)
    assert result.counters.objective_evaluations <= 2
    assert result.base_result.validation_complete

