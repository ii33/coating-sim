"""Mechanical/physical contracts for unscored automatic bandpass starting seeds."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from itertools import islice, product
import json
from typing import Any
from pathlib import Path

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_global as global_search
from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest, freeze_film_synthesis_request
from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
from coat_sim.core.optical_constants import prepare_optical_set
from tests.test_film_auto_synthesis import Service, setup_problem

pytestmark = pytest.mark.algorithm


def bandpass_request(**limits):
    service, base = setup_problem()
    objectives = (
        FilmOptimizationObjective(549.75, 550.25, 3, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=.95, upper_bound=None), metric="T"),
        FilmOptimizationObjective(500., 520., 3, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=.95, upper_bound=None), metric="R"),
        FilmOptimizationObjective(580., 600., 3, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=.95, upper_bound=None), metric="R"),
    )
    request = FilmSynthesisRequest(base.environment, objectives, base.candidate_materials,
                                  **limits)
    return service, freeze_film_synthesis_request(service, request)


def real_bandpass_request():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "benchmarks/film_synthesis/20261021/representative-suite-v2.1.json").read_text(encoding="utf-8"))
    case = next(row for row in manifest["cases"] if row["id"] == "bpf_narrow_visible")
    catalog = json.loads((root / manifest["material_catalog"]).read_text(encoding="utf-8"))
    rows = [dict(id=row["builtin_material_id"], name=row["material_id"], design_ready=True,
                 optical_sets=deepcopy(row["optical_constants"])) for row in catalog["materials"]]
    service = Service(rows)
    environment = new_design(design_id="bandpass-proposal-real-source-test")
    environment["substrate"].update(case["substrate"])
    environment["assume_lossless"] = case["assume_lossless"]
    objectives = tuple(FilmOptimizationObjective(**{**{key: value for key, value in row.items() if key not in {"mode", "target_value", "lower_bound", "upper_bound"}}, "angles_deg": tuple(row["angles_deg"]), "pointwise": FilmObjectiveConstraint(mode="range", lower_bound=row.get("lower_bound"), upper_bound=row.get("upper_bound"))}) for row in case["objectives"])
    request = FilmSynthesisRequest(environment, objectives,
        tuple(FilmNeedleMaterialReference(**row) for row in case["materials"]),
        max_layers=32, minimum_nonzero_thickness_nm=10., max_layer_thickness_nm=500.,
        max_total_thickness_nm=5000.)
    return service, freeze_film_synthesis_request(service, request)


def test_initial_topology_coverage_is_single_coupled_and_end_matched_and_unscored(monkeypatch):
    _, request = bandpass_request(max_layers=32)
    def no_scoring(*args, **kwargs):
        raise AssertionError("a proposal generator cannot perform physical scoring")
    monkeypatch.setattr(synthesis, "evaluate_frozen_structure_candidate", no_scoring)
    proposals = list(islice(global_search.iter_bandpass_proposals(request), 90))
    assert {p.provenance["cavity_count"] for p in proposals} >= {1, 2, 3}
    assert {p.provenance["variant"] for p in proposals} == {
        "single_cavity", "coupled_cavity", "matched_cavity"}
    assert {p.provenance["mirror_pairs"] for p in proposals} >= {1, 2}
    ids = [row["id"] for proposal in proposals for row in proposal.design["front_layers"]]
    assert len(ids) == len(set(ids))
    for proposal in proposals:
        assert proposal.provenance["family"] == "coupled_cavity"
        assert proposal.provenance["search_convention"]
        assert validate_design(proposal.design)
        rows = proposal.design["front_layers"]
        assert [(r["material_id"], r["thickness"]) for r in rows] == [
            (r["material_id"], r["thickness"]) for r in reversed(rows)]


def test_bandpass_prior_keeps_combined_mean_and_pointwise_candidates():
    _, pointwise = bandpass_request(max_layers=5)
    both = replace(pointwise, objectives=tuple(replace(o, mean=replace(o.pointwise))
        for o in pointwise.objectives))

    pointwise_output = list(global_search.iter_bandpass_proposals(pointwise))
    both_output = list(global_search.iter_bandpass_proposals(both))
    assert len(pointwise_output) == len(both_output) == 192
    assert both_output[:12] == pointwise_output[:12]
    assert both_output == pointwise_output
    selected = global_search.bandpass_objectives(both)
    assert len(selected) == 1
    passed, lower_stops, upper_stops = selected[0]
    assert passed is both.objectives[0]
    assert lower_stops == (both.objectives[1],)
    assert upper_stops == (both.objectives[2],)
    assert all(o.pointwise.enabled and o.mean.enabled
               for o in (passed, *lower_stops, *upper_stops))

    mean_only = replace(both, objectives=tuple(replace(o,
        pointwise=replace(o.pointwise, enabled=False)) for o in both.objectives))
    assert global_search.bandpass_objectives(mean_only) == ()
    assert list(global_search.iter_bandpass_proposals(mean_only)) == []


@pytest.mark.parametrize("change", [
    lambda objectives: objectives[:2],
    lambda objectives: objectives[1:],
    lambda objectives: (replace(objectives[0], pointwise=replace(objectives[0].pointwise, lower_bound=None, upper_bound=.2)), *objectives[1:]),
    lambda objectives: (replace(objectives[0], weight=0), *objectives[1:]),
    lambda objectives: (objectives[0], replace(objectives[1], pointwise=replace(objectives[1].pointwise, lower_bound=None, upper_bound=.1)), objectives[2]),
    lambda objectives: (objectives[0], replace(objectives[1], wavelength_max_nm=550.), objectives[2]),
])
def test_bandpass_detection_requires_positive_bracketed_transmission(change):
    _, request = bandpass_request(max_layers=32)
    request = replace(request, objectives=change(request.objectives))
    assert global_search.bandpass_objectives(request) == ()
    assert list(global_search.iter_bandpass_proposals(request)) == []


def test_value_goals_activate_and_equivalent_polarizations_do_not_duplicate_designs():
    _, request = bandpass_request(max_layers=5)
    values = tuple(replace(o, pointwise=replace(o.pointwise, mode="value", target_value=o.pointwise.lower_bound, lower_bound=None))
                   for o in request.objectives)
    single = list(global_search.iter_bandpass_proposals(replace(request, objectives=values)))
    repeated = list(global_search.iter_bandpass_proposals(replace(request, objectives=(*values, *values))))
    assert single and [p.design for p in repeated] == [p.design for p in single]


@pytest.mark.parametrize("limit", [
    {"max_layers": 4}, {"max_layer_thickness_nm": 1.},
    {"max_total_thickness_nm": 1.}, {"minimum_nonzero_thickness_nm": 10000.},
])
def test_explicit_caps_skip_and_report_without_clipping(limit):
    _, request = bandpass_request(**limit)
    events: list[dict[str, Any]] = []
    assert list(global_search.iter_bandpass_proposals(request, report=events.append)) == []
    assert any("constraints" in event["reason"] or "max_layers" in event["reason"] for event in events)


def test_exact_caps_are_inclusive_and_initial_group_box_honors_limits():
    _, request = bandpass_request(max_layers=5)
    first = next(global_search.iter_bandpass_proposals(request))
    thicknesses = [r["thickness"]["value"] for r in first.design["front_layers"]]
    limited = replace(request, minimum_nonzero_thickness_nm=min(thicknesses),
        max_layer_thickness_nm=max(thicknesses), max_total_thickness_nm=sum(thicknesses))
    proposal = next(global_search.iter_bandpass_proposals(limited))
    assert proposal.design == first.design
    initial, lower, upper = global_search.symmetric_thickness_parameters(proposal.design,
        proposal.provenance["symmetry_groups"], minimum_nm=min(thicknesses), maximum_nm=max(thicknesses))
    assert np.all(lower <= initial) and np.all(initial <= upper)
    assert np.min(np.exp(lower)) == pytest.approx(min(thicknesses))
    assert np.max(np.exp(upper)) == pytest.approx(max(thicknesses))


def test_generator_is_lazy_cancellable_during_orders_and_epoch_expands_phase_family():
    _, request = bandpass_request(max_layers=5)
    class Cancelled(Exception):
        pass
    cancelled = False
    def cancel():
        if cancelled:
            raise Cancelled
    iterator = global_search.iter_bandpass_proposals(request, cancel_check=cancel)
    assert next(iterator)
    cancelled = True
    with pytest.raises(Cancelled):
        next(iterator)
    epoch1 = global_search.iter_bandpass_proposals(request, epoch=1)
    assert any(p.provenance["inner_phase_orders"]["high"] == 9 for p in epoch1)


def test_missing_k_or_outside_source_domain_are_reported_and_never_extrapolated():
    _, request = bandpass_request(max_layers=5)
    for missing_k in (True, False):
        snapshot = deepcopy(request.frozen_material_snapshot)
        for row in snapshot["materials"]:
            if row["id"] in {"low", "high"}:
                optical = row["optical_sets"][0]
                if missing_k:
                    optical.pop("k")
                else:
                    optical.update(wavelength_min=530., wavelength_max=570., wavelengths=[530., 550., 570.])
        events: list[dict[str, object]] = []
        assert list(global_search.iter_bandpass_proposals(replace(request,
            frozen_material_snapshot=snapshot), report=events.append)) == []
        assert events and all(e["stage"] == "structure_skipped" for e in events)
        assert request.frozen_material_snapshot != snapshot


def test_nine_layer_high_order_family_has_five_independent_physical_thicknesses():
    _, request = bandpass_request(max_layers=9)
    proposal = next(p for p in global_search.iter_bandpass_proposals(request)
        if len(p.design["front_layers"]) == 9 and p.provenance["cavity_count"] == 1
        and p.provenance["inner_phase_orders"] == {"high": 8, "low": 3}
        and p.provenance["cavity_quarters"] == 3 and not p.provenance["reverse"])
    original = deepcopy(proposal.design)
    groups = proposal.provenance["symmetry_groups"]
    initial, lower, upper = global_search.symmetric_thickness_parameters(original, groups)
    assert initial.shape == lower.shape == upper.shape == (5,)
    values = initial + np.array([.1, -.2, .3, -.1, .2])
    mapped = global_search.symmetric_thickness_design(original, values, symmetry_groups=groups)
    for group, value in zip(groups, values, strict=True):
        assert all(mapped["front_layers"][i]["thickness"]["value"] == pytest.approx(np.exp(value)) for i in group)
    assert [r["id"] for r in mapped["front_layers"]] == [r["id"] for r in original["front_layers"]]
    assert [r["material_id"] for r in mapped["front_layers"]] == [r["material_id"] for r in original["front_layers"]]
    assert original == proposal.design
    assert global_search.structured_thickness_design(original, [0., 0., 0.]) == original


@pytest.mark.parametrize("groups, parameters", [
    (((0,),), [1.]), (((0, 1), (2, 3), (4,)), [1., 1., 1.]),
    (((0, 4), (1, 3), (2,)), [1., 1.]),
    (((0, 4), (1, 3), (2,)), [float("inf"), 1., 1.]),
])
def test_symmetry_mapping_rejects_invalid_partition_material_or_parameter(groups, parameters):
    _, request = bandpass_request(max_layers=5)
    proposal = next(global_search.iter_bandpass_proposals(request))
    with pytest.raises(ValueError):
        global_search.symmetric_thickness_design(proposal.design, parameters, symmetry_groups=groups)


def test_real_nk_sources_and_caps_survive_mechanical_proposal_and_tmm_evaluation():
    _, request = real_bandpass_request()
    before = deepcopy(request.frozen_material_snapshot)
    proposals = list(islice(global_search.iter_bandpass_proposals(request), 40))
    assert proposals and request.environment["assume_lossless"] is False
    for cavities in (1, 2, 3):
        proposal = next(p for p in proposals if p.provenance["cavity_count"] == cavities)
        rows = proposal.design["front_layers"]
        ds = [r["thickness"]["value"] for r in rows]
        assert len(rows) <= 32 and min(ds) >= 10 and max(ds) <= 500 and sum(ds) <= 5000
        score = evaluate_frozen_structure_candidate(synthesis._proxy(request), proposal.design)
        assert np.isfinite(score["score"]["max_error"])
        assert all(np.all(np.isfinite(response["actual_values"])) for response in score["responses"])
    assert request.frozen_material_snapshot == before
    material = next(m for m in before["materials"] if "MgF2" in m["id"])
    _, k, details = prepare_optical_set(material["optical_sets"][0], k_sampling_policy="source")(
        550., "nm", include_k=True, extrapolation_policy="error")
    assert details["k_known"] and k > 0


def test_oblique_phase_uses_frozen_material_incident_index_and_midpoint_angle():
    service, request = bandpass_request(max_layers=5)
    environment = deepcopy(request.environment)
    environment["incident_medium"] = dict(kind="material", material_id="glass", optical_set_id="nk")
    request = freeze_film_synthesis_request(service, replace(request, environment=environment, frozen_material_snapshot=None,
        objectives=tuple(replace(o, angles_deg=(20., 40.)) for o in request.objectives)))
    proposal = next(global_search.iter_bandpass_proposals(request))
    assert proposal.provenance["phase_angle_deg"] == 30.
    center = np.sqrt(549.75 * 550.25)
    q = np.sqrt(2.2 ** 2 - (1.5 * np.sin(np.deg2rad(30.))) ** 2)
    assert proposal.design["front_layers"][0]["thickness"]["value"] == pytest.approx(center / (4 * q))


def test_generic_band_wavelength_scale_changes_thickness_without_material_id_rules():
    _, request = bandpass_request(max_layers=5)
    original = next(global_search.iter_bandpass_proposals(request))
    snapshot = deepcopy(request.frozen_material_snapshot)
    for material in snapshot["materials"]:
        for optical in material["optical_sets"]:
            optical["wavelengths"] = [2 * w for w in optical["wavelengths"]]
            optical["wavelength_min"] *= 2
            optical["wavelength_max"] *= 2
    scaled_request = replace(request, frozen_material_snapshot=snapshot,
        objectives=tuple(replace(o, wavelength_min_nm=2 * o.wavelength_min_nm,
            wavelength_max_nm=2 * o.wavelength_max_nm) for o in request.objectives))
    scaled = next(global_search.iter_bandpass_proposals(scaled_request))
    assert [r["thickness"]["value"] for r in scaled.design["front_layers"]] == pytest.approx([
        2 * r["thickness"]["value"] for r in original.design["front_layers"]])


def test_symmetry_bounds_expand_by_epoch_and_reject_initial_outside_manufacturing_box():
    _, request = bandpass_request(max_layers=5)
    proposal = next(global_search.iter_bandpass_proposals(request))
    groups = proposal.provenance["symmetry_groups"]
    initial, lower, upper = global_search.symmetric_thickness_parameters(proposal.design, groups)
    next_initial, next_lower, next_upper = global_search.symmetric_thickness_parameters(
        proposal.design, groups, epoch=1)
    np.testing.assert_array_equal(initial, next_initial)
    assert np.all(next_lower < lower) and np.all(next_upper > upper)
    with pytest.raises(ValueError, match="outside"):
        global_search.symmetric_thickness_parameters(proposal.design, groups, maximum_nm=1.)


@pytest.mark.parametrize("epoch", [0, 1])
def test_phase_order_schedule_is_deterministic_complete_and_coarse_to_fine(epoch):
    ordered = list(global_search._bandpass_phase_orders(epoch))
    assert ordered == list(global_search._bandpass_phase_orders(epoch))
    expected = set(product(range(1, 9 + 8 * epoch), range(1, 5 + 4 * epoch), range(2, 5 + epoch)))
    assert set(ordered) == expected and len(ordered) == len(expected)
    prefix = ordered[:24]
    endpoints = [(min(v[i] for v in expected), max(v[i] for v in expected)) for i in range(3)]
    for axis, (lower, upper) in enumerate(endpoints):
        assert {lower, upper} <= {orders[axis] for orders in prefix}
        assert any(lower < orders[axis] < upper for orders in prefix)
    # A high-order mirror with an interior cavity order occurs early without
    # assigning a special physical merit to any specific order combination.
    assert any(v[0] == endpoints[0][1] and endpoints[2][0] < v[2] < endpoints[2][1]
               for v in prefix)


def test_first_512_proposals_cover_high_order_and_interior_cavity_orders():
    _, request = bandpass_request(max_layers=32)
    proposals = list(islice(global_search.iter_bandpass_proposals(request), 512))
    assert {p.provenance["inner_phase_orders"]["high"] for p in proposals} >= {1, 8}
    assert {p.provenance["cavity_quarters"] for p in proposals} == {2, 3, 4}
    assert any(len(p.design["front_layers"]) == 9 and p.provenance["cavity_count"] == 1
               and p.provenance["inner_phase_orders"]["high"] == 8 for p in proposals)


def test_first_log_box_covers_factor_eight_and_intersects_manufacturing_bounds():
    _, request = bandpass_request(max_layers=9)
    proposal = next(p for p in global_search.iter_bandpass_proposals(request)
        if len(p.design["front_layers"]) == 9 and p.provenance["cavity_count"] == 1)
    groups = proposal.provenance["symmetry_groups"]
    initial, lower, upper = global_search.symmetric_thickness_parameters(proposal.design, groups)
    np.testing.assert_allclose(lower, initial - np.log(8.))
    np.testing.assert_allclose(upper, initial + np.log(8.))
    bounded_initial, bounded_lower, bounded_upper = global_search.symmetric_thickness_parameters(
        proposal.design, groups, minimum_nm=10., maximum_nm=500.)
    np.testing.assert_array_equal(initial, bounded_initial)
    np.testing.assert_allclose(bounded_lower, np.maximum(lower, np.log(10.)))
    np.testing.assert_allclose(bounded_upper, np.minimum(upper, np.log(500.)))
    for parameters in (bounded_lower, bounded_upper):
        mapped = global_search.symmetric_thickness_design(proposal.design, parameters, symmetry_groups=groups)
        assert all(10. - 1e-12 <= r["thickness"]["value"] <= 500. + 1e-12
                   for r in mapped["front_layers"])
