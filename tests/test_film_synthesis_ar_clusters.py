"""Real-layer AR cluster model contracts, never optimization success claims."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from itertools import islice, product
import math

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_ar_clusters as clusters
from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_design import validate_design
from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
from tests.test_film_synthesis_matching import matching_request, real_matching_request

pytestmark = pytest.mark.algorithm


def proposal_with(*, count=1, pairs=1, **limits):
    _, request = matching_request(**limits)
    return request, next(p for p in clusters.iter_ar_cluster_proposals(request)
        if p.provenance["cluster_count"] == count and p.provenance["pair_counts"] == (pairs,) * count)


def phase_budget(design, metadata, indices=None):
    indices = range(len(design["front_layers"])) if indices is None else indices
    return math.fsum(metadata["layer_slots"][i]["q"] * design["front_layers"][i]["thickness"]["value"] for i in indices)


def test_small_prefix_jointly_covers_materials_cluster_counts_layers_and_total_phase_budgets(monkeypatch):
    _, request = matching_request()
    def no_scoring(*args, **kwargs):
        raise AssertionError("cluster proposal helper must not perform response scoring")
    monkeypatch.setattr(synthesis, "evaluate_frozen_structure_candidate", no_scoring)
    prefix = list(islice(clusters.iter_ar_cluster_proposals(request), 324))
    assert len(prefix) == 324
    assert {p.provenance["cluster_count"] for p in prefix} == {1, 2, 3}
    assert len({frozenset((p.provenance["high"], p.provenance["low"])) for p in prefix}) == 3
    assert {p.provenance["pair_counts"][0] for p in prefix} == {1, 2, 3}
    assert len({p.provenance["requested_total_phase_budget_nm"] for p in prefix}) == 6
    assert {p.provenance["variant"] for p in prefix} == {"non_qw", "clustered"}
    assert {p.provenance["first_role"] for p in prefix} == {"H", "L"}
    ids = [r["id"] for p in prefix for r in p.design["front_layers"]]
    assert len(ids) == len(set(ids))
    for proposal in prefix:
        meta = proposal.provenance
        assert validate_design(proposal.design)
        assert meta["family"] == "ar_cluster" and meta["search_convention"]
        assert phase_budget(proposal.design, meta) == pytest.approx(meta["requested_total_phase_budget_nm"], rel=2e-15)
        assert meta["proposal_total_phase_budget_nm"] == phase_budget(proposal.design, meta)
        assert meta["core_phase_budget_nm"] + meta["edge_phase_budget_nm"] == pytest.approx(meta["requested_total_phase_budget_nm"])
    assert request.environment["front_layers"] == []


@pytest.mark.parametrize("pairs,expected", [(1, 4), (2, 5), (3, 6)])
def test_parameter_dimension_omits_unidentifiable_small_cluster_envelopes(pairs, expected):
    _, proposal = proposal_with(pairs=pairs)
    point, lower, upper = clusters.ar_cluster_parameters(proposal.design, proposal.provenance)
    assert point.shape == lower.shape == upper.shape == (expected,)
    assert np.all(np.isfinite([point, lower, upper]))
    assert np.all(lower < point) and np.all(point < upper)
    assert clusters.ar_cluster_design(proposal.design, point, proposal.provenance) == proposal.design


def test_arbitrary_freed_seed_is_exact_identity_and_preserves_materials_and_layer_ids():
    _, proposal = proposal_with(count=3, pairs=3)
    modified = deepcopy(proposal.design)
    for row, factor in zip(modified["front_layers"], np.exp(np.linspace(-.3, .2, len(modified["front_layers"]))), strict=True):
        row["thickness"]["value"] *= float(factor)
    before = deepcopy(modified)
    point, _, _ = clusters.ar_cluster_parameters(modified, proposal.provenance)
    assert clusters.ar_cluster_design(modified, point, proposal.provenance) == modified
    point[0] += .2
    mapped = clusters.ar_cluster_design(modified, point, proposal.provenance)
    assert [(r["id"], r["material_id"], r["optical_set_id"]) for r in mapped["front_layers"]] == [
        (r["id"], r["material_id"], r["optical_set_id"]) for r in before["front_layers"]]
    assert modified == before
    # A zero envelope preserves the freely refined role's internal ratios.
    role = proposal.provenance["clusters"][0]["high_indices"]
    assert mapped["front_layers"][role[0]]["thickness"]["value"] / mapped["front_layers"][role[1]]["thickness"]["value"] == pytest.approx(
        before["front_layers"][role[0]]["thickness"]["value"] / before["front_layers"][role[1]]["thickness"]["value"])


def test_budget_share_and_relative_envelope_have_independent_physical_meanings():
    _, proposal = proposal_with(pairs=3)
    meta = proposal.provenance
    point, _, _ = clusters.ar_cluster_parameters(proposal.design, meta)
    previous_budget = math.exp(point[0])
    point[:4] += [math.log(1.3), .4, .6, -.2]
    point[-1] += math.log(1.2)
    mapped = clusters.ar_cluster_design(proposal.design, point, meta)
    group = meta["clusters"][0]
    high = phase_budget(mapped, meta, group["high_indices"])
    low = phase_budget(mapped, meta, group["low_indices"])
    assert high + low == pytest.approx(previous_budget * 1.3)
    assert high / (high + low) == pytest.approx(1 / (1 + math.exp(-point[1])))
    for indices in (group["high_indices"], group["low_indices"]):
        old_ratio = proposal.design["front_layers"][indices[-1]]["thickness"]["value"] / proposal.design["front_layers"][indices[0]]["thickness"]["value"]
        new_ratio = mapped["front_layers"][indices[-1]]["thickness"]["value"] / mapped["front_layers"][indices[0]]["thickness"]["value"]
        assert new_ratio / old_ratio == pytest.approx(math.exp(1.2))
    last = meta["boundary_layer_indices"][-1]
    assert mapped["front_layers"][last]["thickness"]["value"] == pytest.approx(
        1.2 * proposal.design["front_layers"][last]["thickness"]["value"])


def test_mapping_does_not_clip_caller_trial_to_manufacturing_limits():
    _, proposal = proposal_with(pairs=2, minimum_nonzero_thickness_nm=10., max_layer_thickness_nm=500.)
    point, lower, upper = clusters.ar_cluster_parameters(proposal.design, proposal.provenance,
                                                        minimum_nm=10., maximum_nm=500.)
    assert np.all(lower <= point) and np.all(point <= upper)
    point[0] += math.log(1000.)
    mapped = clusters.ar_cluster_design(proposal.design, point, proposal.provenance)
    assert max(r["thickness"]["value"] for r in mapped["front_layers"]) > 500.
    with pytest.raises(ValueError, match="outside"):
        clusters.ar_cluster_parameters(proposal.design, proposal.provenance, maximum_nm=1.)


@pytest.mark.parametrize("limit", [{"max_layers": 3}, {"max_layer_thickness_nm": 1.},
    {"max_total_thickness_nm": 1.}, {"minimum_nonzero_thickness_nm": 10000.}])
def test_proposals_report_and_enforce_explicit_manufacturing_limits(limit):
    _, request = matching_request(**{"max_layers": 4, **limit})
    events = []
    assert list(clusters.iter_ar_cluster_proposals(request, report=events.append)) == []
    assert events and all(event["stage"] == "structure_skipped" for event in events)


def test_optional_third_layer_is_distinct_allowed_and_lower_than_the_low_material():
    _, request = matching_request()
    proposal = next(p for p in clusters.iter_ar_cluster_proposals(request) if p.provenance["third_material"] is not None)
    third, high, low = (proposal.provenance[key] for key in ("third_material", "high", "low"))
    assert third not in (high, low)
    assert third in {(m.material_id, m.optical_set_id) for m in request.candidate_materials}
    assert proposal.provenance["boundary_roles"] == ("substrate", "incident", "third_outer")
    assert phase_budget(proposal.design, proposal.provenance) == pytest.approx(proposal.provenance["requested_total_phase_budget_nm"])
    point, _, _ = clusters.ar_cluster_parameters(proposal.design, proposal.provenance)
    assert clusters.ar_cluster_design(proposal.design, point, proposal.provenance) == proposal.design
    only_two = replace(request, candidate_materials=request.candidate_materials[:2], max_layers=4)
    assert all(p.provenance["third_material"] is None for p in clusters.iter_ar_cluster_proposals(only_two))


def test_third_layer_crossing_with_low_material_at_band_edge_rejects_optional_branch():
    _, request = matching_request()
    snapshot = deepcopy(request.frozen_material_snapshot)
    low = next(m for m in snapshot["materials"] if m["id"] == "low")["optical_sets"][0]
    low["n"] = [2., 1., 1.]
    modified = replace(request, frozen_material_snapshot=snapshot)
    assert all(p.provenance["third_material"] is None for p in islice(clusters.iter_ar_cluster_proposals(modified), 512))


def test_unknown_k_opt_in_and_source_domain_rejection_do_not_modify_records():
    _, request = matching_request(max_layers=4)
    snapshot = deepcopy(request.frozen_material_snapshot)
    for material in snapshot["materials"]:
        material["optical_sets"][0].pop("k")
    strict = replace(request, frozen_material_snapshot=snapshot)
    assert list(clusters.iter_ar_cluster_proposals(strict)) == []
    environment = deepcopy(request.environment)
    environment["assume_lossless"] = True
    conditional = replace(strict, environment=environment)
    assert next(clusters.iter_ar_cluster_proposals(conditional))
    outside = deepcopy(snapshot)
    for material in outside["materials"]:
        material["optical_sets"][0].update(wavelength_min=500., wavelength_max=700., wavelengths=[500., 550., 700.])
    assert list(clusters.iter_ar_cluster_proposals(replace(conditional, frozen_material_snapshot=outside))) == []
    assert strict.frozen_material_snapshot == snapshot


def test_generator_cancel_epoch_expansion_and_deterministic_prefix():
    _, request = matching_request()
    class Cancelled(Exception):
        pass
    stopped = True
    def cancel():
        if stopped:
            raise Cancelled
    with pytest.raises(Cancelled):
        next(clusters.iter_ar_cluster_proposals(request, cancel_check=cancel))
    stopped = False
    iterator = clusters.iter_ar_cluster_proposals(request, cancel_check=cancel)
    assert next(iterator)
    stopped = True
    with pytest.raises(Cancelled):
        next(iterator)
    first = list(islice(clusters.iter_ar_cluster_proposals(request), 20))
    assert [p.design for p in first] == [p.design for p in islice(clusters.iter_ar_cluster_proposals(request), 20)]
    expanded = islice(clusters.iter_ar_cluster_proposals(request, epoch=1), 500)
    assert any(p.provenance["pair_counts"][0] == 4 for p in expanded)
    proposal = first[0]
    point, lower, upper = clusters.ar_cluster_parameters(proposal.design, proposal.provenance)
    next_point, next_lower, next_upper = clusters.ar_cluster_parameters(proposal.design, proposal.provenance, epoch=1)
    np.testing.assert_array_equal(point, next_point)
    assert np.all(next_lower < lower) and np.all(next_upper > upper)


@pytest.mark.parametrize("invalid", [np.array([float("nan")]), np.zeros(1)])
def test_mapping_rejects_invalid_coordinate_shapes_or_values(invalid):
    _, proposal = proposal_with()
    with pytest.raises(ValueError):
        clusters.ar_cluster_design(proposal.design, invalid, proposal.provenance)


def test_generic_wavelength_scale_and_current_fixed_q_mapping():
    _, request = matching_request()
    original = next(clusters.iter_ar_cluster_proposals(request))
    snapshot = deepcopy(request.frozen_material_snapshot)
    for material in snapshot["materials"]:
        for optical in material["optical_sets"]:
            optical["wavelengths"] = [2 * w for w in optical["wavelengths"]]
            optical["wavelength_min"] *= 2
            optical["wavelength_max"] *= 2
    scaled = next(clusters.iter_ar_cluster_proposals(replace(request, frozen_material_snapshot=snapshot,
        objectives=tuple(replace(o, wavelength_min_nm=2 * o.wavelength_min_nm,
            wavelength_max_nm=2 * o.wavelength_max_nm) for o in request.objectives))))
    assert [r["thickness"]["value"] for r in scaled.design["front_layers"]] == pytest.approx([
        2 * r["thickness"]["value"] for r in original.design["front_layers"]])
    assert scaled.provenance["requested_total_phase_budget_nm"] == pytest.approx(2 * original.provenance["requested_total_phase_budget_nm"])


def test_oblique_incident_material_q_and_real_source_tmm_are_preserved():
    service, request = matching_request()
    environment = deepcopy(request.environment)
    environment["incident_medium"] = dict(kind="material", material_id="glass", optical_set_id="nk")
    oblique = freeze_film_synthesis_request(service, replace(request, environment=environment,
        frozen_material_snapshot=None, objectives=tuple(replace(o, angles_deg=(20., 40.)) for o in request.objectives)))
    proposal = next(clusters.iter_ar_cluster_proposals(oblique))
    assert proposal.provenance["phase_angle_deg"] == 30.
    slot = proposal.provenance["layer_slots"][0]
    record = next(m for m in oblique.frozen_material_snapshot["materials"] if m["id"] == slot["material"][0])
    n = record["optical_sets"][0]["n"][0]
    assert slot["q"] == pytest.approx(np.sqrt(n ** 2 - (1.5 * np.sin(np.deg2rad(30.))) ** 2))
    real = real_matching_request()
    before = deepcopy(real.frozen_material_snapshot)
    proposals = list(islice(clusters.iter_ar_cluster_proposals(real), 256))
    assert {p.provenance["cluster_count"] for p in proposals} == {1, 2, 3}
    for count in (1, 2, 3):
        proposal = next(p for p in proposals if p.provenance["cluster_count"] == count)
        ds = [r["thickness"]["value"] for r in proposal.design["front_layers"]]
        assert min(ds) >= 10 and max(ds) <= 500 and sum(ds) <= 5000
        result = evaluate_frozen_structure_candidate(synthesis._proxy(real), proposal.design)
        assert np.isfinite(result["score"]["max_error"])
        assert all(np.all(np.isfinite(r["actual_values"])) for r in result["responses"])
    assert before == real.frozen_material_snapshot and real.environment["assume_lossless"] is False


def test_model_rejects_changed_layer_identity_or_swapped_high_low_role_metadata():
    _, proposal = proposal_with(pairs=2)
    point, _, _ = clusters.ar_cluster_parameters(proposal.design, proposal.provenance)
    changed = deepcopy(proposal.design)
    changed["front_layers"][0]["id"] = "different-layer"
    with pytest.raises(ValueError, match="IDs"):
        clusters.ar_cluster_design(changed, point, proposal.provenance)
    metadata = deepcopy(proposal.provenance)
    group = metadata["clusters"][0]
    group["high_indices"], group["low_indices"] = group["low_indices"], group["high_indices"]
    with pytest.raises(ValueError, match="roles"):
        clusters.ar_cluster_parameters(proposal.design, metadata)


@pytest.mark.parametrize("third", [False, True])
def test_lazy_pair_counts_use_actual_hard_layer_capacity_before_enumeration(third):
    counts = list(clusters._iter_pair_counts(10 ** 100, 10, uneven=True, third=third))
    expected = {ps for k in (2, 3) for ps in product(range(1, 5), repeat=k)
                if len(set(ps)) > 1 and 2 * sum(ps) + 2 + int(third) <= 10}
    assert set(counts) == expected and len(counts) == len(expected)
    uniform = list(clusters._iter_pair_counts(10 ** 100, 10, third=third))
    assert all(2 * sum(ps) + 2 + int(third) <= 10 for ps in uniform)
    # The first large-cap uneven tuple is available without materializing the
    # billions of remaining bounded three-cluster compositions.
    assert next(clusters._iter_pair_counts(10 ** 100, 4096, uneven=True)) == (1, 2)


def test_large_epoch_small_layer_cap_and_cancellation_remain_bounded_and_representable():
    _, request = matching_request(max_layers=4, minimum_nonzero_thickness_nm=10.,
                                  max_layer_thickness_nm=500., max_total_thickness_nm=5000.)
    proposal = next(clusters.iter_ar_cluster_proposals(request, epoch=10 ** 100))
    assert len(proposal.design["front_layers"]) == 4
    assert np.isfinite(proposal.provenance["requested_total_phase_budget_nm"])
    assert all(10 <= row["thickness"]["value"] <= 500 for row in proposal.design["front_layers"])
    class Cancelled(Exception):
        pass
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        if calls == 20:
            raise Cancelled
    with pytest.raises(Cancelled):
        list(clusters._iter_pair_counts(10 ** 100, 4096, uneven=True, cancel_check=cancel))
    assert calls == 20
    iterator = clusters.iter_ar_cluster_proposals(request, epoch=10 ** 100, cancel_check=cancel)
    calls = 0
    with pytest.raises(Cancelled):
        next(iterator)
    assert calls == 20


@pytest.mark.parametrize("epoch", [26, 10 ** 100])
def test_large_epoch_logit_and_box_corners_map_to_positive_finite_real_layers(epoch):
    _, proposal = proposal_with(pairs=3)
    point, lower, upper = clusters.ar_cluster_parameters(proposal.design, proposal.provenance, epoch=epoch)
    assert np.all(np.isfinite([point, lower, upper]))
    assert clusters.ar_cluster_design(proposal.design, point, proposal.provenance) == proposal.design
    samples = [np.asarray(corner) for corner in product(*zip(lower, upper, strict=True))]
    samples.extend(np.random.default_rng(21).uniform(lower, upper, (32, len(point))))
    for sample in samples:
        mapped = clusters.ar_cluster_design(proposal.design, sample, proposal.provenance)
        assert all(math.isfinite(row["thickness"]["value"]) and row["thickness"]["value"] > 0
                   for row in mapped["front_layers"])
    # This specifically exercises the former sigmoid(x)==1 / 1-f cancellation.
    high_share = point.copy()
    high_share[1] = upper[1]
    mapped = clusters.ar_cluster_design(proposal.design, high_share, proposal.provenance)
    assert all(mapped["front_layers"][i]["thickness"]["value"] > 0
               for i in proposal.provenance["clusters"][0]["low_indices"])


def test_optional_third_requires_real_n_order_even_when_q_alone_is_lower():
    _, request = matching_request()
    snapshot = deepcopy(request.frozen_material_snapshot)
    for material in snapshot["materials"]:
        optical = material["optical_sets"][0]
        if material["id"] == "low":
            optical["n"] = [1.55] * 3
        elif material["id"] == "glass":
            optical["k"] = [2.] * 3
    oblique = replace(request, frozen_material_snapshot=snapshot,
        objectives=tuple(replace(o, angles_deg=(60.,)) for o in request.objectives))
    q_third = np.sqrt(complex(1.55, 0.) ** 2 - np.sin(np.deg2rad(60.)) ** 2).real
    q_low = np.sqrt(complex(1.5, 2.) ** 2 - np.sin(np.deg2rad(60.)) ** 2).real
    assert q_third < q_low and 1.55 > 1.5
    assert all(p.provenance["third_material"] is None
               for p in islice(clusters.iter_ar_cluster_proposals(oblique), 512))
