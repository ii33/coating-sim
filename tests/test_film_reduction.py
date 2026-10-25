from copy import deepcopy
from dataclasses import replace
from threading import Event
from typing import Any

import numpy as np
import pytest

from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable
from coat_sim.core.film_reduction import (
    FilmReductionRequest, reduce_film_design, freeze_film_reduction_request,
    film_reduction_request_to_payload, film_reduction_request_from_payload,
    film_reduction_result_to_payload, film_reduction_result_from_payload,
    validate_film_reduction_payload, validate_film_reduction_candidate_topology,
)


class Materials:
    def __init__(self):
        self.rows = []
        for material, n in (("glass", 1.5), ("low", 1.2), ("high", 2.1)):
            self.rows.append({"id": material, "name": material, "enabled": True,
                "design_roles": ["substrate"] if material == "glass" else ["film"],
                "design_ready": True, "notes": "synthetic test material", "optical_sets": [{
                    "id": material, "name": material, "kind": "tabulated", "notes": "test",
                    "source": "synthetic test data", "wavelength_unit": "nm",
                    "wavelength_min": 450.0, "wavelength_max": 700.0,
                    "wavelengths": [450.0, 700.0], "n": [n, n], "k": [0.0, 0.0]}]})

    def _records(self, material_ids=None):
        return deepcopy([r for r in self.rows if material_ids is None or r["id"] in material_ids])

    def get(self, identifier):
        return deepcopy(next(r for r in self.rows if r["id"] == identifier))


class Service(FilmDesignService):
    materials: Any
    def __init__(self, response=None):
        self.materials = Materials()
        self.response = response
        self.calls = []

    def simulate(self, design, settings=None, **kwargs):
        self.calls.append((deepcopy(design), deepcopy(settings)))
        if self.response is None:
            return super().simulate(design, settings, **kwargs)
        waves = np.linspace(settings["wavelength_min_nm"], settings["wavelength_max_nm"], settings["samples"])
        value = self.response(design, waves, self.calls)
        return {"wavelength_nm": waves, "R": np.broadcast_to(value, waves.shape), "T": np.ones(waves.shape)}


def layer(identifier, thickness=20.0, material="low"):
    return {"id": identifier, "material_id": material, "optical_set_id": material,
            "thickness": {"value": thickness, "unit": "nm"}, "note": identifier}


def request(rows=None, *, mode="combined", lower=0.0, selected=None, max_iterations=12,
            max_evaluations=80, objective=None, back=None):
    design = new_design("reduction fixture", "fixture")
    design["substrate"].update(material_id="glass", optical_set_id="glass")
    design["front_layers"] = rows if rows is not None else [layer("a")]
    design["back_layers"] = back or []
    design = validate_design(design)
    if selected is None:
        selected = [(side, row["id"]) for side in ("front", "back") for row in design[side + "_layers"]]
    variables = tuple(FilmThicknessVariable(side, identifier, lower, 200.0) for side, identifier in selected)
    objective = objective or FilmOptimizationObjective(450, 700, 3, angles_deg=(0.0, 35.0),
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=1))
    return FilmReductionRequest(FilmOptimizationRequest(design, 7, variables, (objective,),
        max_iterations=max_iterations, max_evaluations=max_evaluations), mode)


@pytest.mark.parametrize("mode", ["layer_count", "combined"])
def test_delete_zero_lower_rows_counts_both_sides_and_real_optical_replay(mode):
    service = Service()
    frozen = freeze_film_reduction_request(service, request(mode=mode, back=[layer("b", 10)]))
    result = reduce_film_design(service, frozen)
    assert result.summary["reduction_achieved"] is True
    assert result.metrics["baseline"]["front"]["layer_count"] == 1
    assert result.metrics["baseline"]["back"]["layer_count"] == 1
    assert result.metrics["candidate"]["layer_count"] == 0
    assert result.base_result.validation_complete
    payload = film_reduction_result_to_payload(frozen, result)
    assert film_reduction_result_from_payload(payload).best_design == result.best_design
    assert validate_film_reduction_payload(service, payload)["reduction"]["summary"]["candidate_feasible"]


def test_thickness_mode_fixed_topology_and_positive_lower_bound():
    service = Service(lambda *_: 0.1)
    result = reduce_film_design(service, request(mode="thickness", lower=7.0))
    assert result.best_thickness_nm == {"a": 7.0}
    assert result.metrics["candidate"]["layer_count"] == 1
    assert result.topology_audit["removed_layer_ids"] == []


def test_unselected_fixed_rows_and_interfaces_cannot_change_or_merge():
    service = Service(lambda *_: 0.1)
    frozen = freeze_film_reduction_request(service, request([layer("fixed-left"), layer("selected"),
        layer("fixed-right")], selected=[("front", "selected")]))
    result = reduce_film_design(service, frozen)
    assert [r["id"] for r in result.best_design["front_layers"]] == ["fixed-left", "fixed-right"]
    assert result.best_design["front_layers"] == [frozen.base_request.design_snapshot["front_layers"][i] for i in (0, 2)]
    tampered = deepcopy(dict(result.best_design))
    tampered["front_layers"][0]["thickness"]["value"] += 1
    with pytest.raises(ValueError, match="unselected"):
        validate_film_reduction_candidate_topology(frozen, tampered)


def test_adjacent_selected_same_reference_merge_conserves_optics_and_respects_survivor_upper():
    def optics(design, *_):
        return 0.1 if sum(r["thickness"]["value"] for r in design["front_layers"]) == 40 else 0.9
    goal = FilmOptimizationObjective(550, 550, 1,
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=0.2))
    service = Service(optics)
    source = request([layer("a"), layer("b")], objective=goal, max_iterations=2)
    result = reduce_film_design(service, source)
    assert result.metrics["candidate"]["layer_count"] == 1
    assert result.metrics["candidate"]["total_thickness_nm"] == 40
    limited = replace(source, base_request=replace(source.base_request,
        variables=tuple(replace(v, upper_nm=30) for v in source.base_request.variables)))
    assert not reduce_film_design(Service(optics), limited).summary["reduction_achieved"]


def test_delete_middle_high_then_merge_remaining_low():
    def optics(design, *_):
        rows = design["front_layers"]
        low_total = sum(r["thickness"]["value"] for r in rows if r["material_id"] == "low")
        return 0.1 if low_total == 40 else 0.9
    goal = FilmOptimizationObjective(550, 550, 1,
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=0.2))
    result = reduce_film_design(Service(optics), request([layer("a"), layer("h", 2, "high"),
        layer("b")], objective=goal, max_iterations=3))
    assert result.metrics["candidate"]["layer_count"] == 1
    assert result.metrics["candidate"]["total_thickness_nm"] == 40


def test_nominal_all_objectives_and_independent_grid_gate_not_solver_tolerance():
    def optics(design, waves, *_):
        if len(design["front_layers"]) == 0:
            return np.where(waves == 512.5, 0.5, 0.1)
        return 0.1
    objective = FilmOptimizationObjective(450, 700, 3,
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=0.2))
    source = request(objective=objective, max_iterations=1)
    source = replace(source, base_request=replace(source.base_request, acceptable_normalized_error=1.0))
    result = reduce_film_design(Service(optics), source)
    assert result.metrics["candidate"]["layer_count"] == 1
    assert result.base_result.validation_raw_coverage[0].wavelength_nm == (450.0, 512.5, 575.0, 637.5, 700.0)


def test_infeasible_source_keeps_unmet_status_no_goal_relaxation():
    objective = FilmOptimizationObjective(550, 550, 1,
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=0.05))
    result = reduce_film_design(Service(lambda *_: 0.1), request(objective=objective, max_iterations=1))
    assert not result.summary["source_feasible"]
    assert not result.summary["candidate_feasible"]
    assert not result.summary["reduction_achieved"]
    assert result.best_thickness_nm == {"a": 20.0}
    assert "source is infeasible" in result.message


def test_first_surface_environment_and_full_back_source_preserved():
    service = Service(lambda *_: 0.1)
    source = request(back=[layer("b")], selected=[("front", "a")], max_iterations=1)
    design = deepcopy(source.base_request.design_snapshot)
    design["first_surface"] = "back"
    design["incident_medium"]["n"] = 1.1
    source = replace(source, base_request=replace(source.base_request, design_snapshot=design))
    result = reduce_film_design(service, source)
    assert result.best_design["back_layers"] == design["back_layers"]
    assert result.best_design["incident_medium"]["n"] == 1.1
    assert all(settings["first_surface"] == "back" for _, settings in service.calls)


@pytest.mark.parametrize("failure", ["cancel", "fail"])
def test_cancel_or_failure_during_candidate_audit_retains_previous_audited_candidate_without_extra_work(failure):
    event = Event()
    def optics(design, waves, calls):
        if len(calls) == 3:
            if failure == "cancel":
                event.set()
            else:
                raise ValueError("injected optical failure")
        return 0.1
    source = request(objective=FilmOptimizationObjective(450, 700, 3,
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=1)))
    service = Service(optics)
    checkpoints: list[Any] = []
    result = reduce_film_design(service, source, cancel_event=event, checkpoint_callback=checkpoints.append)
    assert result.state == ("cancelled" if failure == "cancel" else "failed")
    assert len(service.calls) == 3
    assert result.best_thickness_nm == {"a": 20.0}
    assert result.candidate_verified and result.base_result.validation_complete
    assert len(checkpoints) == 1


def test_strict_request_result_binding_and_tamper_rejection():
    service = Service(lambda *_: 0.1)
    frozen = freeze_film_reduction_request(service, request())
    result = reduce_film_design(service, frozen)
    payload = film_reduction_result_to_payload(frozen, result)
    assert film_reduction_request_from_payload(film_reduction_request_to_payload(frozen)) == frozen
    for mutate in (
        lambda p: p["reduction"].update(request_digest="0" * 64),
        lambda p: p["candidate"]["design"]["front_layers"].append(layer("intruder")),
        lambda p: p["candidate"]["quality"].update(targets_met=False),
        lambda p: p["reduction"]["metrics"]["baseline"].update(total_thickness_nm=0),
        lambda p: p.update(extra=True),
    ):
        tampered = deepcopy(payload)
        mutate(tampered)
        with pytest.raises(ValueError):
            film_reduction_result_from_payload(tampered)


def test_zero_thickness_selected_deletes_and_positive_lower_cannot_delete():
    source = request([layer("zero", 0), layer("thin", 0.01)], max_iterations=2)
    result = reduce_film_design(Service(lambda *_: 0.1), source)
    assert result.metrics["candidate"]["layer_count"] == 0
    frozen = freeze_film_reduction_request(Service(), request(lower=1))
    empty = deepcopy(dict(frozen.base_request.design_snapshot))
    empty["front_layers"] = []
    with pytest.raises(ValueError, match="lower_nm"):
        validate_film_reduction_candidate_topology(frozen, empty)


def test_real_ar_film_thickness_reduction_meets_nontrivial_band_goal():
    goal = FilmOptimizationObjective(500, 600, 5, angles_deg=(0.0, 20.0),
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=0.02))
    source = request([layer("ar", 120)], mode="thickness", lower=100, objective=goal)
    service = Service()
    result = reduce_film_design(service, source)
    assert result.summary["source_feasible"] and result.summary["reduction_achieved"]
    assert result.best_thickness_nm == {"ar": 100.0}
    assert all(max(row.actual_values) < 0.02 for row in result.base_result.raw_coverage)
    assert all(max(row.actual_values) < 0.02 for row in result.base_result.validation_raw_coverage)
    assert len(result.base_result.validation_raw_coverage[0].wavelength_nm) == 9


def test_real_zero_high_spacer_then_same_material_merge_preserves_nontrivial_band():
    goal = FilmOptimizationObjective(500, 600, 5, angles_deg=(0.0,),
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0.025, upper_bound=0.0397))
    source = request([layer("left", 100), layer("zero-high", 0, "high"), layer("right", 100)],
        objective=goal, max_iterations=4)
    result = reduce_film_design(Service(), source)
    assert result.summary["source_feasible"] and result.summary["reduction_achieved"]
    assert result.metrics["candidate"]["layer_count"] == 1
    assert result.metrics["candidate"]["total_thickness_nm"] <= 200
    assert all(0.025 <= value <= 0.0397 for row in result.base_result.validation_raw_coverage for value in row.actual_values)


def test_real_both_side_source_with_back_incidence_is_audited_on_same_grids():
    goal = FilmOptimizationObjective(500, 600, 5, angles_deg=(0.0, 20.0),
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=0.2))
    source = request([layer("front-ar", 120)], back=[layer("back-ar", 110)],
        selected=[("back", "back-ar")], lower=100, mode="thickness", objective=goal)
    design = deepcopy(source.base_request.design_snapshot)
    design["first_surface"] = "back"
    design["substrate"]["sides"] = "double"
    source = replace(source, base_request=replace(source.base_request, design_snapshot=design))
    service = Service()
    result = reduce_film_design(service, source)
    assert result.summary["source_feasible"] and result.summary["reduction_achieved"]
    assert result.best_design["front_layers"] == design["front_layers"]
    assert result.metrics["candidate"]["back"]["total_thickness_nm"] == 100
    payload = film_reduction_result_to_payload(result.frozen_request, result)
    validate_film_reduction_payload(service, payload)
    assert all(settings["first_surface"] == "back" for _, settings in service.calls)


def test_job_executor_freeze_roundtrip_checkpoint_capability_and_cancel_source_binding(tmp_path):
    from coat_sim.core.film_optimization_job import (freeze_job_payload, execute_film_optimization_job,
        serialize_request, deserialize_request)
    from coat_sim.scheduler.result_capability import is_film_optimization_result, job_has_viewable_result
    from coat_sim.scheduler.worker import _film_reply_matches_payload, _completed_film_cancel_reply
    from coat_sim.scheduler.remote_contract import _validate_film_payload
    from coat_sim.scheduler.jobs import freeze_spec
    service = Service(lambda *_: 0.1)
    source = request(max_iterations=1)
    payload = freeze_job_payload(service, source)
    frozen = deserialize_request(payload["request"])
    assert isinstance(frozen, FilmReductionRequest)
    assert serialize_request(frozen) == payload["request"]
    assert _validate_film_payload(payload) == frozen
    assert freeze_spec(service, "film_optimization", {"request": source})["parameters"]["payload"] == payload
    checkpoints: list[Any] = []
    reply = execute_film_optimization_job(service, payload, cancel_event=Event(), checkpoint_callback=checkpoints.append)
    assert len(service.calls) == reply["candidate"]["evaluations"] * 2
    assert is_film_optimization_result(reply)
    assert job_has_viewable_result({"kind": "film_optimization", "state": "succeeded", "result": reply})
    assert checkpoints and _film_reply_matches_payload(checkpoints[-1], payload)
    cancelled = _completed_film_cancel_reply(reply, payload)
    assert film_reduction_result_from_payload(cancelled).state == "cancelled"
    assert job_has_viewable_result({"kind": "film_optimization", "state": "cancelled", "result": cancelled})
    wrong = deepcopy(payload)
    wrong["request"]["mode"] = "thickness"
    assert _completed_film_cancel_reply(reply, wrong) is None


def test_started_grid_counters_record_partial_validation_failure_and_reject_fake_coverage():
    event = Event()
    def optics(design, waves, calls):
        if len(calls) == 4:
            event.set()
        return 0.1
    objective = FilmOptimizationObjective(450, 700, 3,
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=1))
    result = reduce_film_design(Service(optics), request(objective=objective), cancel_event=event)
    assert result.state == "cancelled"
    assert result.base_result.evaluations == 4
    assert result.base_result.validation_evaluations == 2
    payload = film_reduction_result_to_payload(result.frozen_request, result)
    assert film_reduction_result_from_payload(payload).base_result.validation_evaluations == 2
    payload["reduction"]["metrics"]["baseline"]["raw_coverage"][0]["wavelength_nm"][1] += 1
    with pytest.raises(ValueError, match="complete original/independent grid"):
        film_reduction_result_from_payload(payload)


def test_independent_grid_cannot_use_multiplier_one():
    source = request()
    source = replace(source, base_request=replace(source.base_request, validation_samples_multiplier=1))
    with pytest.raises(ValueError, match="independent"):
        freeze_film_reduction_request(Service(), source)


def test_layer_count_mode_does_not_claim_thickness_only_reduction():
    source = request(lower=7, mode="layer_count")
    result = reduce_film_design(Service(lambda *_: 0.1), source)
    assert result.best_thickness_nm == {"a": 20.0}
    assert not result.summary["reduction_achieved"]


def test_real_relevant_layer_deletion_requires_bounded_repair_and_layer_mode_allows_growth():
    goal = FilmOptimizationObjective(540, 560, 3, polarization="s",
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0.015, upper_bound=0.022))
    source = request([layer("survivor", 40), layer("relevant-high", 5, "high")],
        mode="layer_count", objective=goal, max_iterations=4, max_evaluations=80)
    source = replace(source, base_request=replace(source.base_request, variables=(
        replace(source.base_request.variables[0], lower_nm=40), source.base_request.variables[1])))
    service = Service()
    deleted = deepcopy(dict(source.base_request.design_snapshot))
    deleted["front_layers"] = deleted["front_layers"][:1]
    unrepaired = service.simulate(deleted, {"wavelength_min_nm": 540, "wavelength_max_nm": 560,
        "samples": 5, "angle_deg": 0, "polarization": "s"})["R"]
    assert min(unrepaired) > 0.022
    result = reduce_film_design(service, source)
    assert result.summary["source_feasible"] and result.summary["candidate_feasible"]
    assert result.summary["reduction_achieved"] and result.metrics["candidate"]["layer_count"] == 1
    assert result.best_thickness_nm == {"survivor": 60.0}
    assert result.metrics["baseline"]["total_thickness_nm"] == 45.0
    assert result.metrics["candidate"]["total_thickness_nm"] == 60.0
    assert result.summary["saved_thickness_nm"] == -15.0
    assert result.base_result.evaluations == 6
    assert result.base_result.validation_evaluations == 2
    assert all(0.015 <= value <= 0.022 for row in result.base_result.validation_raw_coverage for value in row.actual_values)
    payload = film_reduction_result_to_payload(result.frozen_request, result)
    validate_film_reduction_payload(service, payload)
    joint = reduce_film_design(Service(), replace(source, mode="combined"))
    assert not joint.summary["reduction_achieved"]
    assert joint.metrics["candidate"]["layer_count"] == 2
    assert joint.metrics["candidate"]["total_thickness_nm"] == 45.0


def test_repaired_survivor_may_increase_with_positive_lower_bound_but_fixed_rows_cannot():
    service = Service()
    source = request([layer("fixed", 10), layer("survivor", 40), layer("removed", 5, "high")],
        mode="layer_count", selected=[("front", "survivor"), ("front", "removed")])
    source = replace(source, base_request=replace(source.base_request, variables=(
        replace(source.base_request.variables[0], lower_nm=40), source.base_request.variables[1])))
    frozen = freeze_film_reduction_request(service, source)
    design = deepcopy(dict(frozen.base_request.design_snapshot))
    design["front_layers"] = design["front_layers"][:2]
    design["front_layers"][1]["thickness"]["value"] = 60
    assert validate_film_reduction_candidate_topology(frozen, design)["selected_bounds_satisfied"]
    with pytest.raises(ValueError, match="total physical thickness"):
        validate_film_reduction_candidate_topology(replace(frozen, mode="combined"), design)
    design["front_layers"][0]["thickness"]["value"] = 11
    with pytest.raises(ValueError, match="unselected"):
        validate_film_reduction_candidate_topology(frozen, design)


@pytest.mark.parametrize("kind", ["coat_sim.film_reduction_request", "coat_sim.film_structure_optimization_request", "unknown"])
def test_reduction_request_rejects_nested_independent_request_kinds(kind):
    frozen = freeze_film_reduction_request(Service(), request())
    payload = film_reduction_request_to_payload(frozen)
    payload["base_request"]["kind"] = kind
    with pytest.raises(ValueError, match="base_request"):
        film_reduction_request_from_payload(payload)


def test_reduction_result_rejects_nonmapping_validation_metadata():
    result = reduce_film_design(Service(lambda *_: 0.1), request(max_iterations=1))
    payload = film_reduction_result_to_payload(result.frozen_request, result)
    payload["solver_validation"] = []
    with pytest.raises(ValueError, match="mapping"):
        film_reduction_result_from_payload(payload)


def _meaningful_repair_source(*, max_evaluations=80):
    goal = FilmOptimizationObjective(540, 560, 3, polarization="s",
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0.015, upper_bound=0.022))
    source = request([layer("survivor", 40), layer("relevant-high", 5, "high")],
        mode="layer_count", objective=goal, max_iterations=4, max_evaluations=max_evaluations)
    return replace(source, base_request=replace(source.base_request, variables=(
        replace(source.base_request.variables[0], lower_nm=40), source.base_request.variables[1])))


@pytest.mark.parametrize("cancel_at", [4, 5])
def test_cancel_inside_real_survivor_repair_stops_forward_calls_and_preserves_prior_audit(cancel_at):
    event = Event()
    class CancellingService(Service):
        def simulate(self, design, settings=None, **kwargs):
            response = super().simulate(design, settings, **kwargs)
            if len(self.calls) == cancel_at:
                event.set()
            return response
    source = _meaningful_repair_source()
    service = CancellingService()
    checkpoints: list[Any] = []
    result = reduce_film_design(service, source, cancel_event=event, checkpoint_callback=checkpoints.append)
    assert result.state == "cancelled"
    assert len(service.calls) == cancel_at
    assert result.base_result.evaluations == cancel_at
    assert result.base_result.validation_evaluations == 1
    assert result.base_result.validation_complete and result.candidate_verified
    assert result.best_design == result.frozen_request.base_request.design_snapshot
    assert result.metrics["candidate"] == result.metrics["baseline"]
    assert len(checkpoints) == 1
    assert film_reduction_result_from_payload(film_reduction_result_to_payload(result.frozen_request, result)).state == "cancelled"


@pytest.mark.parametrize("budget", [3, 4, 5])
def test_global_tiny_budget_counts_inner_real_repair_and_stops_all_remaining_proposals(budget):
    service = Service()
    result = reduce_film_design(service, _meaningful_repair_source(max_evaluations=budget))
    assert result.state == "completed"
    assert len(service.calls) == result.base_result.evaluations <= budget
    assert "Evaluation budget reached" in result.message
    assert result.base_result.evaluations == (2 if budget == 3 else budget)
    assert result.base_result.validation_evaluations == (1 if budget == 3 else 2)
    assert result.best_design == result.frozen_request.base_request.design_snapshot
    assert result.candidate_verified and result.base_result.validation_complete
    assert not result.summary["reduction_achieved"]
    assert film_reduction_result_from_payload(film_reduction_result_to_payload(result.frozen_request, result)).base_result.evaluations <= budget


def test_same_material_id_different_optical_set_never_forms_direct_merge_proposal():
    from coat_sim.core.film_reduction import _proposals
    service = Service(lambda *_: 0.1)
    low = next(row for row in service.materials.rows if row["id"] == "low")
    alternate = deepcopy(low["optical_sets"][0])
    alternate.update(id="low-distinct-nk", n=[1.8, 1.8])
    low["optical_sets"].append(alternate)
    rows = [layer("left"), layer("right")]
    rows[1]["optical_set_id"] = "low-distinct-nk"
    frozen = freeze_film_reduction_request(service, request(rows, mode="layer_count"))
    proposals = list(_proposals(frozen, frozen.base_request.design_snapshot, 0.25))
    assert len(proposals) == 2  # Two deletion proposals; no algebraic merge.
    assert all(len(candidate["front_layers"]) == 1 for candidate in proposals)
    assert all(candidate["front_layers"][0]["thickness"]["value"] == 20 for candidate in proposals)
    assert {candidate["front_layers"][0]["optical_set_id"] for candidate in proposals} == {"low", "low-distinct-nk"}


def _chunked_repair_source():
    pointwise = FilmObjectiveConstraint(enabled=False)
    reflection = FilmOptimizationObjective(450, 700, 10002, angles_deg=(0.0, 20.0), polarization="s",
        pointwise=pointwise, mean=FilmObjectiveConstraint(mode="range", lower_bound=0.015, upper_bound=0.022))
    transmission = FilmOptimizationObjective(500, 600, 3, angles_deg=(0.0, 30.0), polarization="p", metric="T",
        pointwise=pointwise, mean=FilmObjectiveConstraint(mode="range", lower_bound=0.98, upper_bound=1.0))
    source = _meaningful_repair_source()
    return replace(source, base_request=replace(source.base_request, objectives=(reflection, transmission)))


def _chunked_fake_response(design, _waves, _calls):
    rows = design["front_layers"]
    return 0.018 if len(rows) == 2 else (0.019 if rows[0]["thickness"]["value"] >= 60 else 0.03)


def test_real_optimizer_protocol_counts_multiobjective_multiangle_chunked_repair():
    service = Service(_chunked_fake_response)
    result = reduce_film_design(service, _chunked_repair_source())
    training_frame_samples = [10001, 1, 10001, 1, 3, 3]
    validation_frame_samples = [10001, 10001, 1, 10001, 10001, 1, 5, 5]
    expected_samples = training_frame_samples + validation_frame_samples + training_frame_samples * 3 + validation_frame_samples
    assert [settings["samples"] for _, settings in service.calls] == expected_samples
    assert len(service.calls) == 40
    assert result.base_result.evaluations == 6
    assert result.base_result.validation_evaluations == 2
    assert result.summary["reduction_achieved"] and result.best_thickness_nm == {"survivor": 60.0}
    assert [len(row.wavelength_nm) for row in result.base_result.raw_coverage] == [10002, 10002, 3, 3]
    assert [len(row.wavelength_nm) for row in result.base_result.validation_raw_coverage] == [20003, 20003, 5, 5]


@pytest.mark.parametrize(("stop_at", "expected_grids", "expected_validation"), [(16, 3, 1), (22, 4, 1), (34, 6, 2)])
def test_chunked_inner_repair_cancel_midgrid_never_starts_extra_optics(stop_at, expected_grids, expected_validation):
    event = Event()
    def response(design, waves, calls):
        if len(calls) == stop_at:
            event.set()
        return _chunked_fake_response(design, waves, calls)
    service = Service(response)
    result = reduce_film_design(service, _chunked_repair_source(), cancel_event=event)
    assert len(service.calls) == stop_at
    assert result.state == "cancelled"
    assert result.base_result.evaluations == expected_grids
    assert result.base_result.validation_evaluations == expected_validation
    assert result.candidate_verified and result.base_result.validation_complete
    assert result.metrics["candidate"] == result.metrics["baseline"]


@pytest.mark.parametrize(("stop_at", "expected_grids", "expected_validation"), [(16, 3, 1), (34, 6, 2)])
def test_chunked_inner_repair_first_score_or_holdout_failure_keeps_previous_complete_audit(stop_at, expected_grids, expected_validation):
    def response(design, waves, calls):
        if len(calls) == stop_at:
            raise ValueError("injected chunk failure")
        return _chunked_fake_response(design, waves, calls)
    service = Service(response)
    result = reduce_film_design(service, _chunked_repair_source())
    assert len(service.calls) == stop_at
    assert result.state == "failed"
    assert result.base_result.evaluations == expected_grids
    assert result.base_result.validation_evaluations == expected_validation
    assert result.candidate_verified and result.base_result.validation_complete
    assert result.metrics["candidate"] == result.metrics["baseline"]
