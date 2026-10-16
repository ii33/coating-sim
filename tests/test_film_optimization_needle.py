from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from threading import Event
from typing import Any

import pytest

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_optimization import (
    FilmOptimizationObjective,
    FilmOptimizationRequest,
    FilmThicknessVariable,
)
from coat_sim.core.film_optimization_needle import (
    FilmNeedleMaterialReference,
    FilmNeedleOptimizationRequest,
    freeze_film_optimization_needle_request,
    optimize_film_design_with_needle,
)
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import default_material_profile


def _optical(identifier: str, refractive_index: float) -> dict:
    return {
        "id": identifier,
        "name": identifier,
        "kind": "tabulated",
        "notes": "Synthetic needle optimizer test data",
        "source": "Analytical test fixture",
        "wavelength_unit": "nm",
        "wavelength_min": 450.0,
        "wavelength_max": 700.0,
        "wavelengths": [450.0, 700.0],
        "n": [refractive_index, refractive_index],
        "k": [0.0, 0.0],
    }


def _service(tmp_path):
    library = MaterialLibrary(tmp_path)
    for identifier, category, optical_set in (
        ("needle_glass", "substrate", _optical("glass_n15", 1.5)),
        ("needle_film", "film", _optical("film_n123", 1.23)),
        ("needle_high", "film", _optical("high_n20", 2.0)),
    ):
        record = default_material_profile("Synthetic " + identifier, id=identifier)
        record.update(category=category, optical_constants=[optical_set])
        library.write(record)
    service = FilmDesignService(tmp_path)
    design = service.new_design()
    design["substrate"].update(material_id="needle_glass", optical_set_id="glass_n15")
    design["front_layers"] = [{
        "id": "existing-stable-layer",
        "material_id": "needle_film",
        "optical_set_id": "film_n123",
        "thickness": {"value": 100.0, "unit": "nm"},
        "note": "pre-existing layer",
    }]
    return service, design


def _objective(target_value: float = 0.0):
    return FilmOptimizationObjective(
        wavelength_min_nm=550.0,
        wavelength_max_nm=550.0,
        wavelength_samples=1,
        angles_deg=(0.0,),
        polarization="unpolarized",
        metric="R",
        mode="value",
        target_value=target_value,
    )


def _request(design, objective, *, base_options=None, needle_options=None):
    base = FilmOptimizationRequest(
        design_snapshot=deepcopy(design),
        design_revision=23,
        variables=(),
        objectives=(objective,),
        max_evaluations=8,
        max_iterations=4,
        **(base_options or {}),
    )
    return FilmNeedleOptimizationRequest(
        base_request=base,
        candidate_materials=(FilmNeedleMaterialReference("needle_high", "high_n20"),),
        min_thickness_nm=20.0,
        max_thickness_nm=80.0,
        **(needle_options or {}),
    )


def test_needle_insertion_is_finite_deterministic_and_preserves_existing_ids(tmp_path):
    service, design = _service(tmp_path)
    original = deepcopy(design)
    expected = deepcopy(design)
    expected["front_layers"].insert(0, {
        "id": "expected-only-for-target",
        "material_id": "needle_high",
        "optical_set_id": "high_n20",
        "thickness": {"value": 20.0, "unit": "nm"},
        "note": "expected target fixture",
    })
    response = service.simulate(expected, {
        "wavelength_min_nm": 550.0,
        "wavelength_max_nm": 550.0,
        "samples": 1,
        "angle_deg": 0.0,
        "polarization": "unpolarized",
        "first_surface": "front",
    })
    request = _request(design, _objective(response["R"][0]))
    result = optimize_film_design_with_needle(service, request)
    second = optimize_film_design_with_needle(service, request)

    assert result.structure_changed
    assert result.accepted_insertions == 1
    assert len(result.added_layer_ids) == 1
    assert result.added_layer_ids == second.added_layer_ids
    assert result.max_normalized_error == 0.0
    assert result.evaluations <= request.base_request.max_evaluations
    assert [row["id"] for row in result.best_design["front_layers"] if row["id"] != result.added_layer_ids[0]] == [
        "existing-stable-layer"
    ]
    inserted = next(row for row in result.best_design["front_layers"]
                    if row["id"] == result.added_layer_ids[0])
    assert 20.0 <= inserted["thickness"]["value"] <= 80.0
    assert inserted["thickness"]["unit"] == "nm"
    assert design == original


def test_needle_enforces_layer_budget_and_freezes_more_than_eight_variables(tmp_path):
    service, design = _service(tmp_path)
    objective = _objective()
    no_room = _request(design, objective, needle_options={"max_total_layers": 1})
    with pytest.raises(ValueError, match="no room"):
        optimize_film_design_with_needle(service, no_room)

    # Seven valid base variables plus room for two insertions exceeded the old
    # shared eight-variable ceiling. Freeze now retains the full request.
    template = deepcopy(design["front_layers"][0])
    design["front_layers"] = []
    for index in range(7):
        layer = deepcopy(template)
        layer["id"] = f"base-layer-{index}"
        design["front_layers"].append(layer)
    request = _request(
        design, objective, needle_options={"max_added_layers": 2},
    )
    request = replace(request, base_request=replace(
        request.base_request, max_evaluations=12))
    variables = tuple(
        FilmThicknessVariable("front", row["id"], 90.0, 110.0)
        for row in design["front_layers"]
    )
    request = replace(
        request,
        base_request=replace(request.base_request, variables=variables),
    )

    frozen = freeze_film_optimization_needle_request(service, request)

    assert len(frozen.base_request.variables) == 7
    assert frozen.base_request.variables == variables
    assert frozen.max_added_layers == 2


@pytest.mark.parametrize(("max_added", "max_total", "accepted"), [
    (2, 3, 2),  # Multiple rounds are an existing finite-needle capability.
    (3, 3, 2),  # The total-layer budget clips the requested insertion count.
    (2, 2, 1),
])
def test_needle_runs_multiple_insertions_with_shared_budgets(
    tmp_path, max_added, max_total, accepted,
):
    service, design = _service(tmp_path)
    original = deepcopy(design)
    target_design = deepcopy(design)
    for index in range(2):
        target_design["front_layers"].insert(0, {
            "id": f"target-layer-{index}",
            "material_id": "needle_high",
            "optical_set_id": "high_n20",
            "thickness": {"value": 20.0, "unit": "nm"},
            "note": "physical target fixture",
        })
    settings = {
        "wavelength_min_nm": 550.0, "wavelength_max_nm": 550.0,
        "samples": 1, "angle_deg": 0.0, "polarization": "unpolarized",
        "first_surface": "front",
    }
    target = service.simulate(target_design, settings)["R"][0]
    request = _request(design, _objective(target), needle_options={
        "max_added_layers": max_added, "max_total_layers": max_total,
    })
    # A narrow physical manufacturing interval prevents a single new layer
    # from absorbing the second layer's optical thickness during refinement.
    request = replace(
        request, min_thickness_nm=20.0, max_thickness_nm=20.001,
        base_request=replace(request.base_request, max_evaluations=32),
    )
    checkpoints: list[dict[str, Any]] = []
    result = optimize_film_design_with_needle(
        service, request, checkpoint_callback=checkpoints.append,
    )
    repeated = optimize_film_design_with_needle(service, request)

    assert result.accepted_insertions == accepted
    assert len(result.added_layer_ids) == accepted
    assert len(result.best_design["front_layers"]) == 1 + accepted <= max_total
    assert result.added_layer_ids == repeated.added_layer_ids
    assert result.best_design == repeated.best_design
    assert result.evaluations <= request.base_request.max_evaluations
    assert result.iterations <= request.base_request.max_iterations
    assert len(checkpoints) == accepted
    previous_error = abs(service.simulate(design, settings)["R"][0] - target)
    for checkpoint in checkpoints:
        assert checkpoint["max_normalized_error"] < previous_error
        previous_error = checkpoint["max_normalized_error"]
    for row in result.best_design["front_layers"]:
        if row["id"] in result.added_layer_ids:
            assert 20.0 <= row["thickness"]["value"] <= 20.001
        else:
            assert row == original["front_layers"][0]
    assert result.max_normalized_error == pytest.approx(abs(
        service.simulate(result.best_design, settings)["R"][0] - target,
    ))
    assert design == original


def test_unsupported_back_surface_scope_is_explicit(tmp_path):
    service, design = _service(tmp_path)
    design["substrate"]["sides"] = "double"
    with pytest.raises(ValueError, match="single-sided front-surface"):
        optimize_film_design_with_needle(service, _request(design, _objective()))


def test_failed_inserted_trials_roll_back_without_mutating_design(tmp_path):
    service, design = _service(tmp_path)

    class RejectInsertedService(FilmDesignService):
        def simulate(self, trial_design, settings=None):
            if any(row["material_id"] == "needle_high" for row in trial_design["front_layers"]):
                raise ValueError("synthetic inserted-material failure")
            return super().simulate(trial_design, settings)

    rejecting_service = RejectInsertedService(tmp_path)
    original = deepcopy(design)
    result = optimize_film_design_with_needle(
        rejecting_service, _request(design, _objective(target_value=1.0)))

    assert not result.structure_changed
    assert result.added_layer_ids == ()
    assert result.failed_evaluations == 2  # one per insertion interface
    assert result.rolled_back_insertions == 2
    assert result.evaluations == 3  # exact baseline plus both failed trials
    assert result.best_design == original
    assert design == original


def test_cancel_after_baseline_keeps_exact_best_without_probing(tmp_path):
    service, design = _service(tmp_path)
    cancel = Event()
    cancel.set()
    result = optimize_film_design_with_needle(
        service, _request(design, _objective()), cancel_event=cancel)

    assert result.state == "cancelled"
    assert result.evaluations == 1
    assert not result.structure_changed
    assert result.best_design == design


def test_frozen_candidate_materials_are_reused_without_catalog_lookup(tmp_path):
    service, design = _service(tmp_path)
    request = _request(design, _objective())
    frozen = freeze_film_optimization_needle_request(service, request)
    records = {row["id"]: row for row in frozen.base_request.frozen_material_snapshot["materials"]}
    assert "high_n20" in {row["id"] for row in records["needle_high"]["optical_sets"]}

    def unexpected_lookup(_identifier):
        raise AssertionError("the frozen request must not rescan candidate materials")

    service.materials.get = unexpected_lookup
    result = optimize_film_design_with_needle(service, frozen)
    assert result.evaluations >= 1
