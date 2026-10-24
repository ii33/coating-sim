"""Generic HR prior construction uses source optics and explicit boundaries."""
from copy import deepcopy
from dataclasses import replace
import cmath
from itertools import islice
import math
from typing import Any

import pytest

from coat_sim.core import film_synthesis_hr_priors as priors
from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_design import MAX_LAYERS, validate_design
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from coat_sim.core.film_synthesis_absorption import sample_synthesis_optical
from coat_sim.core.optical_constants import prepare_optical_set
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def hr_request(*, low=400., high=850., angles=(0., 40.), polarization="unpolarized",
               max_layers=10, absorbing=True, names=("alpha-source", "beta-source")):
    service, request = setup_problem()
    rows = service.materials.records
    renamed = {"low": names[0], "high": names[1]}
    for old, new in renamed.items():
        rows[new] = rows.pop(old)
        rows[new]["id"] = new
    for name, record in rows.items():
        optical = record["optical_sets"][0]
        optical.update(wavelength_min=350., wavelength_max=1000.,
            wavelengths=[350., 550., 1000.],
            source="synthetic-source-identity https://example.invalid/synthetic-optics")
        optical["n"] = ([1.4, 1.42, 1.46] if name == names[0] else
                        [2.3, 2.25, 2.15] if name == names[1] else [1.5] * 3)
        optical["k"] = ([.005, .01, .02] if name == names[0] else
                        [.03, .04, .06] if name == names[1] else [0.] * 3) if absorbing else [0.] * 3
    objectives = (FilmOptimizationObjective(low, high, 3, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=.99, upper_bound=1.), angles_deg=angles, polarization=polarization),)
    request = replace(request, objectives=objectives, max_layers=max_layers,
        candidate_materials=tuple(FilmNeedleMaterialReference(name, "nk") for name in names),
        seed_designs=())
    return freeze_film_synthesis_request(service, request)


def proposals(request):
    return list(priors.iter_dual_substack_hr_proposals(request))


def test_first_prior_uses_derived_edges_balanced_counts_and_recommended_order():
    request = hr_request()
    original = deepcopy(request)
    output = proposals(request)
    assert len(output) == 6
    assert [p.provenance["fraction"] for p in output] == [1 / 3, 1 / 3, 1 / 4, 1 / 4, 1 / 2, 1 / 2]
    first = output[0]
    metadata = first.provenance
    assert metadata["high"] == ("beta-source", "nk")
    assert metadata["low"] == ("alpha-source", "nk")
    assert metadata["pair_counts"] == (3, 2)
    assert metadata["layer_count"] == 10
    assert metadata["material_order"] == "HL" and metadata["center_order"] == "low_then_high"
    assert metadata["order_scope"] == "incident_to_substrate"
    assert metadata["storage_order"] == "substrate_outward"
    assert [r["material_id"] for r in first.design["front_layers"]] == ["alpha-source", "beta-source"] * 5
    assert [r["material_id"] for r in output[1].design["front_layers"]] == ["beta-source", "alpha-source"] * 5
    contrast = min(w["ratio"] for w in metadata["witness_conditions"])
    assert metadata["contrast"] == contrast
    delta = 4 / math.pi * math.asin((contrast - 1) / (contrast + 1))
    assert metadata["fractional_stopband"] == pytest.approx(delta)
    assert metadata["centers_nm"] == pytest.approx((400. * (1 + delta / 3), 850. / (1 + delta / 3)))
    assert validate_design(first.design) == first.design
    assert request == original


def test_pure_hr_prior_keeps_combined_mean_and_pointwise_candidates():
    pointwise = hr_request()
    both = replace(pointwise, objectives=tuple(replace(o, mean=replace(o.pointwise))
        for o in pointwise.objectives))

    pointwise_output = proposals(pointwise)
    both_output = proposals(both)
    assert len(pointwise_output) == len(both_output) == 6
    assert both_output[:4] == pointwise_output[:4]
    assert both_output == pointwise_output
    assert priors.pure_hr_prior_eligible(both)
    assert all(o.pointwise.enabled and o.mean.enabled for o in both.objectives)
    assert both.objectives == tuple(replace(o, mean=replace(o.pointwise))
        for o in pointwise.objectives)

    mean_only = replace(both, objectives=tuple(replace(o,
        pointwise=replace(o.pointwise, enabled=False)) for o in both.objectives))
    assert not priors.pure_hr_prior_eligible(mean_only)
    assert proposals(mean_only) == []


def test_witnesses_cover_every_requested_angle_and_polarization_with_passive_k():
    request = hr_request()
    first = next(priors.iter_dual_substack_hr_proposals(request))
    witnesses = first.provenance["witness_conditions"]
    assert len(witnesses) == 12
    assert {w["polarization"] for w in witnesses} == {"s", "p"}
    assert {w["angle_deg"] for w in witnesses} == {0., 40.}
    assert sorted({w["wavelength_nm"] for w in witnesses}) == pytest.approx([400., math.sqrt(400. * 850.), 850.])
    sources = first.provenance["optical_sources"]
    assert {s["material_id"] for s in sources} == {"alpha-source", "beta-source"}
    assert all(s["source"] == "synthetic-source-identity https://example.invalid/synthetic-optics" for s in sources)
    assert first.provenance["search_convention"] is True
    materials = {m["id"]: m for m in request.frozen_material_snapshot["materials"]}
    # Reconstruct the research incident-side convention from production storage.
    # Verify every material/thickness pair, including the unequal 3/2 pair split.
    incident_layers = list(reversed(first.design["front_layers"]))
    incident_centers = ([first.provenance["centers_nm"][0]] * 6
                        + [first.provenance["centers_nm"][1]] * 4)
    for layer, center in zip(incident_layers, incident_centers, strict=True):
        optical = materials[layer["material_id"]]["optical_sets"][0]
        n, k, _ = sample_synthesis_optical(prepare_optical_set(optical), center, assume_unknown_k_zero=False)
        assert k > 0
        q = cmath.sqrt(complex(n, k) ** 2 - math.sin(math.radians(20.)) ** 2)
        assert q.imag >= 0 and q.real > 0
        assert layer["thickness"]["value"] == pytest.approx(center / (4 * q.real))
    lossless = next(priors.iter_dual_substack_hr_proposals(hr_request(absorbing=False)))
    assert first.design["front_layers"][0]["thickness"] != lossless.design["front_layers"][0]["thickness"]


@pytest.mark.parametrize("mutation", ["seed", "T", "AR", "value", "upper", "double", "front", "back", "one_material", "no_limit", "too_few"])
def test_pure_seed_free_single_sided_explicit_boundary_is_required(mutation):
    request = hr_request()
    environment = deepcopy(dict(request.environment))
    if mutation == "seed":
        request = replace(request, seed_designs=(environment,))
    elif mutation in {"T", "AR", "value", "upper"}:
        objective = request.objectives[0]
        if mutation == "T":
            changed = replace(objective, metric="T")
        else:
            pointwise_changes: dict[str, dict[str, Any]] = {
                "AR": dict(lower_bound=0., upper_bound=.1),
                "value": dict(mode="value", target_value=.99, lower_bound=None, upper_bound=None),
                "upper": dict(upper_bound=.999),
            }
            changed = replace(objective, pointwise=replace(objective.pointwise, **pointwise_changes[mutation]))
        request = replace(request, objectives=(*request.objectives, changed))
    elif mutation == "double":
        environment["substrate"]["sides"] = "double"
        request = replace(request, environment=environment)
    elif mutation in {"front", "back"}:
        environment[mutation + "_layers"] = [{"id": "existing-layer"}]
        request = replace(request, environment=environment)
    elif mutation == "one_material":
        request = replace(request, candidate_materials=request.candidate_materials[:1])
    else:
        request = replace(request, max_layers=None if mutation == "no_limit" else 3)
    assert not priors.pure_hr_prior_eligible(request)
    assert proposals(request) == []


@pytest.mark.parametrize("constraint", ["minimum_nonzero_thickness_nm", "max_layer_thickness_nm", "max_total_thickness_nm"])
def test_every_explicit_thickness_constraint_rejects_without_clamping(constraint):
    request = hr_request()
    limit = 1000. if constraint == "minimum_nonzero_thickness_nm" else 1.
    reports: list[dict[str, Any]] = []
    assert list(priors.iter_dual_substack_hr_proposals(replace(request, **{constraint: limit}), report=reports.append)) == []
    assert reports and all(r["reason"] == "explicit physical thickness constraints" for r in reports)


def test_band_angle_layer_and_material_changes_are_derived_not_name_routed():
    first = proposals(hr_request(low=400., high=850., angles=(0., 40.), max_layers=11))[0]
    changed = proposals(hr_request(low=450., high=950., angles=(10., 50.), max_layers=14,
        names=("other-low", "other-high")))[0]
    assert len(first.design["front_layers"]) == 10
    assert len(changed.design["front_layers"]) == 14
    assert changed.provenance["pair_counts"] == (4, 3)
    assert changed.provenance["phase_angle_deg"] == 30.
    assert changed.provenance["centers_nm"] != first.provenance["centers_nm"]
    assert {r["material_id"] for r in changed.design["front_layers"]} == {"other-low", "other-high"}


def test_format_limit_caps_explicit_request_without_default_scan(monkeypatch):
    request = hr_request()
    capped = next(priors.iter_dual_substack_hr_proposals(replace(request, max_layers=MAX_LAYERS + 100)))
    assert len(capped.design["front_layers"]) == MAX_LAYERS // 2 * 2
    def unexpected(*args, **kwargs):
        raise AssertionError("Missing explicit boundary must not sample materials")
    monkeypatch.setattr(priors, "prepare_optical_set", unexpected)
    assert proposals(replace(request, max_layers=None)) == []


def test_iterator_preparation_is_lazy_and_stops_without_swallowing_valueerror(monkeypatch):
    request = hr_request()
    prepared = []
    prepare = priors.prepare_optical_set
    def tracked(*args, **kwargs):
        prepared.append(1)
        return prepare(*args, **kwargs)
    monkeypatch.setattr(priors, "prepare_optical_set", tracked)
    stream = priors.iter_dual_substack_hr_proposals(request)
    assert prepared == []
    assert len(list(islice(stream, 1))) == 1 and len(prepared) == 2
    stream.close()
    calls: list[int] = []
    reports: list[dict[str, Any]] = []
    stop = ValueError("caller stop is not invalid physics")
    def cancel():
        calls.append(1)
        if len(calls) == 7:
            raise stop
    with pytest.raises(ValueError) as failure:
        next(priors.iter_dual_substack_hr_proposals(request, cancel_check=cancel, report=reports.append))
    assert failure.value is stop and reports == []


@pytest.mark.parametrize("invalid", ["crossed_centers", "crossed_contrast", "source_domain", "absorbing_incident", "unknown_k"])
def test_invalid_physical_priors_report_skip_without_extrapolation(invalid):
    request = hr_request(absorbing=False)
    snapshot = deepcopy(request.frozen_material_snapshot)
    assert snapshot is not None
    rows = {m["id"]: m for m in snapshot["materials"]}
    if invalid == "crossed_centers":
        request = replace(request, objectives=(replace(request.objectives[0],
            wavelength_min_nm=550., wavelength_max_nm=550., wavelength_samples=1),))
    elif invalid == "crossed_contrast":
        rows["beta-source"]["optical_sets"][0]["n"] = [1.1, 2.5, 1.1]
        rows["alpha-source"]["optical_sets"][0]["n"] = [1.5] * 3
        request = replace(request, objectives=(replace(request.objectives[0], angles_deg=(0.,)),))
    elif invalid == "source_domain":
        request = replace(request, objectives=(replace(request.objectives[0], wavelength_min_nm=300.),))
    elif invalid == "absorbing_incident":
        environment = deepcopy(dict(request.environment))
        environment["incident_medium"] = dict(kind="material", material_id="alpha-source", optical_set_id="nk")
        rows["alpha-source"]["optical_sets"][0]["k"] = [.1] * 3
        request = replace(request, environment=environment)
    else:
        rows["beta-source"]["optical_sets"][0]["k"] = None
    request = replace(request, frozen_material_snapshot=snapshot)
    reports: list[dict[str, Any]] = []
    assert list(priors.iter_dual_substack_hr_proposals(request, report=reports.append)) == []
    assert reports and all(r["stage"] == "structure_skipped" for r in reports)
