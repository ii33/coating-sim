"""Current strict band contracts retain raw spectral evidence across JSON."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from coat_sim.core.film_design import new_design
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint as Constraint
from coat_sim.core.film_optimization import (
    FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable,
)
from coat_sim.core.film_optimization_structure import (
    FilmStructureOptimizationRequest, evaluate_frozen_structure_candidate,
    film_structure_request_from_payload, film_structure_request_to_payload,
    film_structure_result_from_payload, film_structure_result_to_payload,
    freeze_film_structure_optimization_request, optimize_film_design_with_structure,
    validate_film_structure_payload,
)
from coat_sim.core.film_structure_payload_contract import (
    validate_structure_request_payload_schema, validate_structure_result_payload_schema,
)
from tests.test_film_auto_synthesis import Service

pytestmark = pytest.mark.algorithm


@pytest.fixture(scope="module")
def band_payload():
    records = []
    for name, index in (("glass", 1.5), ("film", 2.2)):
        records.append(dict(id=name, name=name, design_ready=True, optical_sets=[dict(
            id="nk", name=name, kind="tabulated", notes="synthetic payload fixture",
            source="synthetic numerical oracle; not external material evidence", wavelength_unit="nm",
            wavelength_min=450., wavelength_max=700., wavelengths=[450., 550., 700.],
            n=[index] * 3, k=[0.] * 3)]))
    service = Service(records)
    design = new_design(design_id="band-payload-fixture")
    design["substrate"].update(material_id="glass", optical_set_id="nk")
    design["front_layers"] = [dict(id="layer", material_id="film", optical_set_id="nk",
                                  thickness=dict(value=80., unit="nm"), note="fixture")]
    objective = FilmOptimizationObjective(500., 600., 3, angles_deg=(0., 35.),
        pointwise=Constraint(enabled=False), mean=Constraint(lower_bound=0., upper_bound=None))
    base = FilmOptimizationRequest(design, 7, (FilmThicknessVariable("front", "layer", 60., 100.),),
        (objective,), max_iterations=1, max_evaluations=6, acceptable_normalized_error=0.)
    request = freeze_film_structure_optimization_request(service, FilmStructureOptimizationRequest(
        base, mode="growth_only", min_thickness_nm=20., max_thickness_nm=150.,
        max_total_thickness_nm=200., max_total_layers=2, candidate_budget=1))
    evaluation = evaluate_frozen_structure_candidate(request, request.base_request.design_snapshot,
                                                      with_jacobian=False)
    physical = evaluation["score"]["coverage"]
    means = evaluation["score"]["errors"]
    lower = min(row.actual for row in means)
    local_minimum = min(min(row.actual_values) for row in physical)
    assert local_minimum < lower
    threshold = (lower + local_minimum) / 2
    objective = replace(objective, mean=Constraint(lower_bound=threshold, upper_bound=None))
    request = replace(request, base_request=replace(request.base_request, objectives=(objective,)))
    result = optimize_film_design_with_structure(None, request)
    payload = json.loads(json.dumps(film_structure_result_to_payload(request, result)))
    return request, result, payload


def test_real_physics_nested_mean_only_roundtrip_keeps_full_raw_grids(band_payload):
    request, result, payload = band_payload
    assert payload["schema_version"] == payload["request"]["schema_version"] == 2
    validate_structure_request_payload_schema(payload["request"])
    validate_structure_result_payload_schema(payload)
    parsed_request = film_structure_request_from_payload(film_structure_request_to_payload(request))
    assert parsed_request.base_request.objectives == request.base_request.objectives
    parsed = film_structure_result_from_payload(payload)
    assert parsed.base_result.raw_coverage == result.base_result.raw_coverage
    assert parsed.base_result.per_condition_errors == result.base_result.per_condition_errors
    assert len(parsed.base_result.per_condition_errors) == 2
    assert len(parsed.base_result.validation_per_condition_errors) == 2
    assert [len(row.wavelength_nm) for row in parsed.base_result.raw_coverage] == [3, 3]
    assert [len(row.wavelength_nm) for row in parsed.base_result.validation_raw_coverage] == [5, 5]
    assert all(row.wavelength_nm is None and row.reduction == "mean"
               for row in parsed.base_result.per_condition_errors)
    assert result.base_result.targets_met
    assert min(row.actual_values[0] for row in result.base_result.raw_coverage) < request.base_request.objectives[0].mean.lower_bound
    replay = validate_film_structure_payload(None, payload)
    assert replay["partial_result"]["candidate_verified"]


@pytest.mark.parametrize("path", [("schema_version",), ("request", "schema_version")])
def test_schema_one_is_rejected_without_legacy_fallback(band_payload, path):
    payload = deepcopy(band_payload[2])
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = 1
    with pytest.raises(ValueError, match="schema_version"):
        film_structure_result_from_payload(payload)


@pytest.mark.parametrize("mutation", ["oldflat", "missing_constraint", "unknown_constraint", "text_target", "strict_value"])
def test_request_rejects_legacy_flat_and_malformed_nested_constraints(band_payload, mutation):
    payload = deepcopy(band_payload[2]["request"])
    objective = payload["base_request"]["objectives"][0]
    if mutation == "oldflat":
        objective["mode"] = "range"
    elif mutation == "missing_constraint":
        del objective["mean"]
    elif mutation == "unknown_constraint":
        objective["mean"]["unknown"] = True
    elif mutation == "text_target":
        objective["mean"]["lower_bound"] = "0.2"
    else:
        objective["mean"].update(mode="value", target_value=.2, lower_bound=None,
                                 upper_bound=None, strict_lower=True)
    with pytest.raises(ValueError):
        film_structure_request_from_payload(payload)


@pytest.mark.parametrize("mutation", ["missing_raw", "sparse_raw", "wrong_grid", "duplicate_angle", "fake_mean", "center_mean", "fake_status", "missing_holdout"])
def test_mean_rows_cannot_claim_raw_coverage_or_forge_active_acceptance(band_payload, mutation):
    payload = deepcopy(band_payload[2])
    candidate = payload["candidate"]
    if mutation == "missing_raw":
        candidate["raw_coverage"] = []
    elif mutation == "sparse_raw":
        candidate["raw_coverage"][0]["wavelength_nm"] = [500., 600.]
        candidate["raw_coverage"][0]["actual_values"] = [.5, .5]
    elif mutation == "wrong_grid":
        candidate["raw_coverage"][0]["wavelength_nm"][1] += 1.
    elif mutation == "duplicate_angle":
        candidate["raw_coverage"][1]["angle_deg"] = 0.
    elif mutation == "fake_mean":
        candidate["per_condition_errors"][0]["actual"] += .01
    elif mutation == "center_mean":
        candidate["per_condition_errors"][0]["wavelength_nm"] = 550.
    elif mutation == "fake_status":
        candidate["quality"]["solver_acceptable"] = False
    else:
        candidate["validation_raw_coverage"] = []
    with pytest.raises(ValueError):
        film_structure_result_from_payload(payload)


@pytest.mark.parametrize("path,value", [
    (("candidate", "quality", "solver_acceptable"), "true"),
    (("candidate", "quality", "validation_solver_acceptable"), 1),
    (("candidate", "raw_coverage", 0, "objective_index"), True),
    (("candidate", "raw_coverage", 0, "actual_values", 0), "0.3"),
    (("candidate", "per_condition_errors", 0, "strict_lower"), 0),
])
def test_result_rejects_boolean_integer_and_text_coercion(band_payload, path, value):
    payload = deepcopy(band_payload[2])
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        film_structure_result_from_payload(payload)


def test_structural_material_and_budget_checks_remain_authoritative(band_payload):
    payload = deepcopy(band_payload[2])
    payload["candidate"]["design"]["front_layers"][0]["material_id"] = "unfrozen"
    with pytest.raises(ValueError):
        validate_film_structure_payload(None, payload)
    payload = deepcopy(band_payload[2])
    payload["structure"]["counters"]["objective_evaluations"] = 100
    with pytest.raises(ValueError, match="budget"):
        film_structure_result_from_payload(payload)
