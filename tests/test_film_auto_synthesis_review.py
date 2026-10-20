"""Independent physical and acceptance checks for first-stage synthesis."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from threading import Event

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
from coat_sim.core.film_design import new_design
from coat_sim.core.film_design_service import simulate_frozen
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from coat_sim.core.film_optimization_structure import _insert_at_site, _site_records
from coat_sim.core.refractiveindex_local import RefractiveIndexLocalStore, packaged_store_root
from tests.test_film_auto_synthesis import Service, setup_problem

pytestmark = pytest.mark.algorithm


def real_problem(polarization="s"):
    store = RefractiveIndexLocalStore(packaged_store_root())
    records = []
    for material in ("SiO2", "MgF2", "Ta2O5"):
        row = next(r for r in store.search(material + " Rodriguez", limit=10)["entries"]
                   if r["source_path"] == f"main/{material}/nk/Rodriguez-de Marcos.yml")
        optical = store.convert_entry(row["entry_id"], optical_set_id="nk", name=material)["optical_set"]
        records.append(dict(id=material, name=material, design_ready=True, optical_sets=[optical]))
    service = Service(records)
    environment = new_design(design_id="independent-synthesis-review")
    environment["substrate"].update(material_id="SiO2", optical_set_id="nk")
    objectives = (
        FilmOptimizationObjective(500, 650, 5, (0., 33., 57.), polarization, "R", "value",
                                  target_value=.02, weight=2.5, normalization_scale=.3),
        FilmOptimizationObjective(500, 650, 5, (0., 33., 57.), polarization, "T", "range",
                                  lower_bound=.98, upper_bound=1., weight=.4, normalization_scale=.7),
    )
    request = FilmSynthesisRequest(environment, objectives,
        tuple(FilmNeedleMaterialReference(m, "nk") for m in ("MgF2", "Ta2O5")),
        max_cycles=1, max_evaluations=2)
    return service, synthesis.freeze_film_synthesis_request(service, request)


def independent_loss(value, objectives):
    total = 0.
    for row in value["score"]["errors"]:
        objective = objectives[row.objective_index]
        if objective.mode == "value":
            assert objective.target_value is not None
            violation = abs(row.actual - objective.target_value)
        else:
            violation = max(0.,
                0. if objective.lower_bound is None else objective.lower_bound - row.actual,
                0. if objective.upper_bound is None else row.actual - objective.upper_bound)
        total += objective.weight * (violation / objective.normalization_scale) ** 2
    return total


@pytest.mark.parametrize("polarization", ["s", "p", "unpolarized"])
@pytest.mark.parametrize("location", ["substrate", "interior", "incident"])
def test_real_nk_multiangle_needle_derivative_and_exact_zero_equivalence(polarization, location):
    service, request = real_problem(polarization)
    assert request.frozen_material_snapshot is not None
    original_materials = deepcopy(service.materials.records)
    design = deepcopy(dict(request.environment))
    design["front_layers"] = [dict(id="host", material_id="MgF2", optical_set_id="nk",
        thickness={"value": 87., "unit": "nm"}, note="fixed real dispersive host")]
    sites = _site_records(design, request.frozen_material_snapshot, 1)
    site = sites[{"substrate": 0, "interior": 1, "incident": 2}[location]]
    reference = FilmNeedleMaterialReference("Ta2O5", "nk")
    zero = _insert_at_site(design, site, reference, 0., "needle", "right", zero=True)
    proxy = synthesis._proxy(request)
    probe = synthesis.evaluate_frozen_structure_candidate(proxy, zero, variable_ids=("needle",),
        with_jacobian=True, zero_thickness_layer_ids=("needle",))
    baseline = synthesis.evaluate_frozen_structure_candidate(proxy, design)
    loss0 = independent_loss(baseline, request.objectives)
    assert probe["smooth_loss"] == pytest.approx(loss0, abs=2e-13)
    differences = []
    for step in (1e-4, 5e-5):
        trial = _insert_at_site(design, site, reference, step, "needle", "right")
        value = synthesis.evaluate_frozen_structure_candidate(proxy, trial)
        differences.append((independent_loss(value, request.objectives) - loss0) / step)
    extrapolated = 2 * differences[1] - differences[0]
    assert probe["gradient_by_layer_id"]["needle"] == pytest.approx(extrapolated, rel=2e-5, abs=2e-9)
    for response in probe["responses"]:
        objective = request.objectives[response["objective_index"]]
        # This is the ordinary frozen forward service, independent of scoring/Jacobian assembly.
        frozen = service.freeze_design(design, dict(wavelength_min_nm=500., wavelength_max_nm=650.,
            samples=5, angle_deg=response["angle_deg"], polarization=polarization))
        direct = simulate_frozen(".", frozen)
        np.testing.assert_allclose(response["actual_values"], direct[objective.metric], rtol=0, atol=5e-13)
        np.testing.assert_allclose(np.array(direct["R"]) + direct["T"] + direct["A"], 1., atol=5e-13)
    assert service.materials.records == original_materials
    assert len(design["front_layers"]) == 1


def test_small_objective_weight_cannot_hide_a_violated_condition_or_change_tolerance():
    service, request = setup_problem(upper=.01)
    difficult = replace(request.objectives[0], weight=1e-20, normalization_scale=.1)
    irrelevant = replace(difficult, upper_bound=1., weight=1e20)
    result = synthesis.synthesize_film_design(service, replace(request,
        objectives=(difficult, irrelevant), max_evaluations=2))
    assert result.training_score is not None and result.validation_score is not None
    assert result.training_score["weighted_rms_error"] < 1e-18
    assert result.training_score["max_error"] == pytest.approx(.3)
    assert result.validation_score["max_error"] == pytest.approx(.3)
    assert result.state == "not_found" and not result.targets_met


def test_holdout_has_new_wavelengths_and_recomputes_all_angle_polarization_conditions():
    service, request = real_problem("p")
    result = synthesis.synthesize_film_design(service, request)
    assert result.training_score is not None and result.validation_score is not None
    for index, objective in enumerate(request.objectives):
        for angle in objective.angles_deg:
            training = [r for r in result.training_score["errors"]
                        if r.objective_index == index and r.angle_deg == angle]
            validation = [r for r in result.validation_score["errors"]
                          if r.objective_index == index and r.angle_deg == angle]
            assert len(training) == 5 and len(validation) >= 33
            assert {r.wavelength_nm for r in training} < {r.wavelength_nm for r in validation}
            direct = simulate_frozen(".", service.freeze_design(request.environment,
                dict(wavelength_min_nm=500., wavelength_max_nm=650., samples=17,
                     angle_deg=angle, polarization="p")))
            dense_waves = set(np.linspace(500., 650., 17))
            dense_rows = sorted((r for r in validation if r.wavelength_nm in dense_waves), key=lambda r: r.wavelength_nm)
            assert len(dense_rows) == 17
            np.testing.assert_allclose([r.actual for r in dense_rows], direct[objective.metric], atol=2e-13)
    assert result.validation_evaluations == 1 and result.evaluations == 2


def test_pre_cancelled_run_and_result_mutation_never_write_back_to_request_or_library():
    service, request = setup_problem(upper=.05)
    environment = deepcopy(request.environment)
    materials = deepcopy(service.materials.records)
    event = Event()
    event.set()
    cancelled = synthesis.synthesize_film_design(service, request, cancel_event=event)
    assert cancelled.state == "cancelled" and cancelled.candidate_design is None
    assert cancelled.evaluations == 0 and not cancelled.validation_complete
    result = synthesis.synthesize_film_design(service, replace(request, design_revision=73))
    assert result.candidate_design is not None and result.targets_met
    assert result.design_revision == 73
    result.candidate_design["substrate"]["note"] = "only the returned nested candidate changes"
    assert request.environment == environment
    assert result.frozen_request.environment == environment
    assert service.materials.records == materials
