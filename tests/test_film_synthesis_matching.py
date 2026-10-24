"""AR matching proposal contracts; these seeds are never acceptance claims."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from itertools import islice
import json
import math
from pathlib import Path

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_matching as matching
from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest, freeze_film_synthesis_request
from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
from coat_sim.core.optical_constants import prepare_optical_set
from tests.test_film_auto_synthesis import Service, setup_problem

pytestmark = pytest.mark.algorithm


def matching_request(**limits):
    service, base = setup_problem()
    for material in service.materials.records.values():
        material["optical_sets"][0].update(wavelength_min=350., wavelength_max=950.,
                                          wavelengths=[350., 550., 950.])
    request = FilmSynthesisRequest(base.environment,
        (FilmOptimizationObjective(400., 900., 5, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=.02), metric="R"),),
        (*base.candidate_materials, FilmNeedleMaterialReference("glass", "nk")), **limits)
    return service, freeze_film_synthesis_request(service, request)


def real_matching_request():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "benchmarks/film_synthesis/20261021/representative-suite-v2.1.json").read_text(encoding="utf-8"))
    case = next(row for row in manifest["cases"] if row["id"] == "ar_ultrabroad_visible")
    catalog = json.loads((root / manifest["material_catalog"]).read_text(encoding="utf-8"))
    service = Service([dict(id=row["builtin_material_id"], name=row["material_id"], design_ready=True,
        optical_sets=deepcopy(row["optical_constants"])) for row in catalog["materials"]])
    environment = new_design(design_id="matching-real-source-test")
    environment["substrate"].update(case["substrate"])
    environment["assume_lossless"] = case["assume_lossless"]
    objectives = tuple(FilmOptimizationObjective(**{**{key: value for key, value in row.items() if key not in {"mode", "target_value", "lower_bound", "upper_bound"}}, "angles_deg": tuple(row["angles_deg"]), "pointwise": FilmObjectiveConstraint(mode="range", lower_bound=row.get("lower_bound"), upper_bound=row.get("upper_bound"))}) for row in case["objectives"])
    request = FilmSynthesisRequest(environment, objectives,
        tuple(FilmNeedleMaterialReference(**row) for row in case["materials"]),
        minimum_nonzero_thickness_nm=case["min_thickness_nm"],
        max_layer_thickness_nm=case["max_thickness_nm"], max_layers=case["max_layers"],
        max_total_thickness_nm=case["max_total_thickness_nm"])
    return freeze_film_synthesis_request(service, request)


def test_first_sweep_covers_all_materials_pair_counts_wavelengths_and_directions_without_scoring(monkeypatch):
    _, request = matching_request(minimum_nonzero_thickness_nm=10.)
    def no_scoring(*args, **kwargs):
        raise AssertionError("proposal helper must not evaluate optical responses")
    monkeypatch.setattr(synthesis, "evaluate_frozen_structure_candidate", no_scoring)
    first = list(islice(matching.iter_matching_proposals(request), 144))
    assert len(first) == 144
    assert {p.provenance["pairs"] for p in first} == {1, 2, 3}
    material_pairs = {frozenset((p.provenance["high"], p.provenance["low"])) for p in first}
    assert len(material_pairs) == 3
    assert len({p.provenance["reference_wavelength_nm"] for p in first}) == 4
    assert {p.provenance["first_role"] for p in first} == {"L", "H"}
    assert {p.provenance["reverse"] for p in first} == {False, True}
    assert all(p.provenance["thin_mode"] == "explicit_minimum" for p in first)
    assert all(p.provenance["terminal_quarters"] == 1 for p in first)
    assert len({row["id"] for p in first for row in p.design["front_layers"]}) == sum(
        len(p.design["front_layers"]) for p in first)
    for proposal in first:
        assert validate_design(proposal.design)
        assert proposal.provenance["family"] == "matching"
        assert proposal.provenance["search_convention"]
        ds = [row["thickness"]["value"] for row in proposal.design["front_layers"]]
        assert ds.count(10.) == 2 * proposal.provenance["pairs"]
    assert request.environment["front_layers"] == []


@pytest.mark.parametrize("common_changes, pointwise_changes", [
    ({"metric": "T"}, {}), ({"weight": 0.}, {}),
    ({}, {"lower_bound": .98, "upper_bound": .99}),
    ({}, {"lower_bound": .9, "upper_bound": None}), ({}, {"upper_bound": 1.}),
    ({}, {"mode": "value", "lower_bound": None, "upper_bound": None, "target_value": .99}),
])
def test_matching_requires_pure_positive_weight_ar_objectives(common_changes, pointwise_changes):
    _, request = matching_request()
    objective = request.objectives[0]
    changed = replace(request, objectives=(replace(objective, **common_changes,
        pointwise=replace(objective.pointwise, **pointwise_changes)),))
    assert matching.matching_objectives(changed) == ()
    assert list(matching.iter_matching_proposals(changed)) == []


def test_zero_ceiling_and_low_value_activate_and_single_material_returns_no_pair():
    _, request = matching_request()
    for objective in (replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, upper_bound=0.)),
                      replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, mode="value", lower_bound=None, upper_bound=None, target_value=.01))):
        assert next(matching.iter_matching_proposals(replace(request, objectives=(objective,))))
    assert list(matching.iter_matching_proposals(replace(request,
        candidate_materials=request.candidate_materials[:1]))) == []


def test_no_implicit_manufacturing_minimum_and_exact_physical_minimum_is_available():
    _, request = matching_request()
    proposal = next(matching.iter_matching_proposals(request))
    assert proposal.provenance["thin_quarter_fraction"] == 1 / 16
    assert min(r["thickness"]["value"] for r in proposal.design["front_layers"]) < 10.
    _, limited = matching_request(minimum_nonzero_thickness_nm=10.)
    assert next(matching.iter_matching_proposals(limited)).provenance["thin_physical_nm"] == 10.


@pytest.mark.parametrize("limit", [
    {"max_layers": 2}, {"max_layer_thickness_nm": 1.},
    {"max_total_thickness_nm": 1.}, {"minimum_nonzero_thickness_nm": 10000.},
])
def test_manufacturing_caps_are_reported_and_never_bypassed(limit):
    _, request = matching_request(**limit)
    events: list[dict[str, object]] = []
    assert list(matching.iter_matching_proposals(request, report=events.append)) == []
    assert events and all(e["stage"] == "structure_skipped" for e in events)


def test_exact_caps_are_inclusive_and_no_thickness_clipping_occurs():
    _, request = matching_request(minimum_nonzero_thickness_nm=10.)
    first = next(matching.iter_matching_proposals(request))
    ds = [row["thickness"]["value"] for row in first.design["front_layers"]]
    limited = replace(request, max_layers=3, max_layer_thickness_nm=max(ds),
                      max_total_thickness_nm=math.fsum(ds))
    assert next(matching.iter_matching_proposals(limited)).design == first.design


def test_epoch_expands_counts_and_profiles_and_generator_is_deterministic():
    _, request = matching_request()
    initial = list(islice(matching.iter_matching_proposals(request), 144))
    assert [p.design for p in initial] == [p.design for p in islice(matching.iter_matching_proposals(request), 144)]
    expanded = list(islice(matching.iter_matching_proposals(request, epoch=1), 192))
    assert {p.provenance["pairs"] for p in expanded} == {1, 2, 3, 4}
    rest = matching.iter_matching_proposals(replace(request, max_layers=3))
    assert any(p.provenance["variant"] == "quarter_wave_matching" for p in rest)
    assert any(p.provenance["variant"] == "graded_matching"
               for p in matching.iter_matching_proposals(replace(request, max_layers=5)))


def test_cancellation_before_first_yield_and_while_iterator_is_suspended():
    _, request = matching_request()
    class Cancelled(Exception):
        pass
    cancelled = True
    def cancel():
        if cancelled:
            raise Cancelled
    with pytest.raises(Cancelled):
        next(matching.iter_matching_proposals(request, cancel_check=cancel))
    cancelled = False
    iterator = matching.iter_matching_proposals(request, cancel_check=cancel)
    assert next(iterator)
    cancelled = True
    with pytest.raises(Cancelled):
        next(iterator)


def test_missing_k_requires_explicit_assumption_and_source_domain_remains_strict():
    _, request = matching_request()
    assert request.frozen_material_snapshot is not None
    snapshot = deepcopy(request.frozen_material_snapshot)
    for material in snapshot["materials"]:
        for optical in material["optical_sets"]:
            optical.pop("k")
    strict = replace(request, frozen_material_snapshot=snapshot)
    assert list(matching.iter_matching_proposals(strict)) == []
    environment = deepcopy(strict.environment)
    environment["assume_lossless"] = True
    assert next(matching.iter_matching_proposals(replace(strict, environment=environment)))
    outside = deepcopy(snapshot)
    for material in outside["materials"]:
        material["optical_sets"][0].update(wavelength_min=500., wavelength_max=700.,
                                          wavelengths=[500., 550., 700.])
    assert list(matching.iter_matching_proposals(replace(strict, environment=environment,
        frozen_material_snapshot=outside))) == []
    assert snapshot == strict.frozen_material_snapshot


def test_real_source_nk_caps_and_true_tmm_remain_intact():
    request = real_matching_request()
    assert request.frozen_material_snapshot is not None
    before = deepcopy(request.frozen_material_snapshot)
    proposals = list(islice(matching.iter_matching_proposals(request), 144))
    assert {p.provenance["pairs"] for p in proposals} == {1, 2, 3}
    assert len({frozenset((p.provenance["high"], p.provenance["low"])) for p in proposals}) == 3
    for count in (1, 2, 3):
        proposal = next(p for p in proposals if p.provenance["pairs"] == count)
        ds = [r["thickness"]["value"] for r in proposal.design["front_layers"]]
        assert min(ds) >= 10. and max(ds) <= 500. and sum(ds) <= 5000.
        result = evaluate_frozen_structure_candidate(synthesis._proxy(request), proposal.design)
        assert np.isfinite(result["score"]["max_error"])
        assert all(np.all(np.isfinite(r["actual_values"])) for r in result["responses"])
    assert request.frozen_material_snapshot == before and request.environment["assume_lossless"] is False
    source = next(row for row in before["materials"] if "MgF2" in row["id"])["optical_sets"][0]
    _, k, details = prepare_optical_set(source, k_sampling_policy="source")(
        550., "nm", include_k=True, extrapolation_policy="error")
    assert details["k_known"] and k > 0


def test_phase_thickness_uses_material_incident_index_at_midpoint_angle():
    service, request = matching_request()
    environment = deepcopy(request.environment)
    environment["incident_medium"] = dict(kind="material", material_id="glass", optical_set_id="nk")
    oblique = freeze_film_synthesis_request(service, replace(request, environment=environment,
        frozen_material_snapshot=None,
        objectives=tuple(replace(o, angles_deg=(20., 40.)) for o in request.objectives)))
    proposal = next(matching.iter_matching_proposals(oblique))
    assert proposal.provenance["phase_angle_deg"] == 30.
    row = proposal.design["front_layers"][0]
    assert oblique.frozen_material_snapshot is not None
    material = next(m for m in oblique.frozen_material_snapshot["materials"] if m["id"] == row["material_id"])
    n, k, _ = prepare_optical_set(material["optical_sets"][0], k_sampling_policy="source")(
        proposal.provenance["reference_wavelength_nm"], "nm", include_k=True, extrapolation_policy="error")
    q = np.sqrt(complex(n, k) ** 2 - (1.5 * np.sin(np.deg2rad(30.))) ** 2).real
    assert row["thickness"]["value"] == pytest.approx(
        proposal.provenance["reference_wavelength_nm"] / (4 * q * 16))


def test_wavelength_scale_changes_physical_thickness_without_named_band_rules():
    _, request = matching_request()
    original = next(matching.iter_matching_proposals(request))
    snapshot = deepcopy(request.frozen_material_snapshot)
    for material in snapshot["materials"]:
        for optical in material["optical_sets"]:
            optical["wavelengths"] = [2 * w for w in optical["wavelengths"]]
            optical["wavelength_min"] *= 2
            optical["wavelength_max"] *= 2
    scaled = next(matching.iter_matching_proposals(replace(request,
        frozen_material_snapshot=snapshot,
        objectives=tuple(replace(o, wavelength_min_nm=2 * o.wavelength_min_nm,
            wavelength_max_nm=2 * o.wavelength_max_nm) for o in request.objectives))))
    assert [row["thickness"]["value"] for row in scaled.design["front_layers"]] == pytest.approx([
        2 * row["thickness"]["value"] for row in original.design["front_layers"]])
    assert scaled.provenance["pairs"] == original.provenance["pairs"]
