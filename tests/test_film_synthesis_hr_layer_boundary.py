"""Mathematical HR topology coverage; no physical optimizer calls."""
from dataclasses import replace
import math

import pytest

from coat_sim.core import film_synthesis_global as search
from tests.test_film_synthesis_global import real_reflector_request

pytestmark = pytest.mark.algorithm


def broad_request(**limits):
    from copy import deepcopy
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    from coat_sim.core.refractiveindex_local import RefractiveIndexLocalStore, packaged_store_root
    service, request = real_reflector_request(**limits)
    store = RefractiveIndexLocalStore(packaged_store_root())
    entry = next(row for row in store.search("SiO2", limit=100)["entries"]
                 if row["source_path"] == "main/SiO2/nk/Rodriguez-de Marcos.yml")
    optical = store.convert_entry(entry["entry_id"], optical_set_id="nk", name="SiO2")["optical_set"]
    service.materials.records["SiO2"] = dict(id="SiO2", name="SiO2", design_ready=True,
                                            optical_sets=[optical])
    environment = deepcopy(request.environment)
    environment["substrate"].update(material_id="SiO2", optical_set_id="nk")
    request = freeze_film_synthesis_request(service, replace(request,
        environment=environment, frozen_material_snapshot=None))
    objective = replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, lower_bound=.98), wavelength_min_nm=400., wavelength_max_nm=700., angles_deg=(0., 30.), polarization="s")
    return replace(request, objectives=(objective, replace(objective, polarization="p")))


@pytest.mark.parametrize(("cap", "boundary"), [(32, 32), (31, 30)])
def test_explicit_layer_boundary_adds_legal_even_topology(cap, boundary):
    request = broad_request(max_layers=cap, minimum_nonzero_thickness_nm=10.,
                            max_layer_thickness_nm=500., max_total_thickness_nm=5000.)
    proposals = list(search.iter_reflector_proposals(request))
    edge = [p for p in proposals if len(p.design["front_layers"]) == boundary]
    assert edge
    assert {p.provenance["family"] for p in edge} == {"periodic", "multistack", "chirped"}
    assert {p.provenance["reverse"] for p in edge} == {False, True}
    assert all(len(p.design["front_layers"]) <= cap for p in proposals)
    for proposal in proposals:
        thicknesses = [row["thickness"]["value"] for row in proposal.design["front_layers"]]
        assert all(10. <= d <= 500. for d in thicknesses)
        assert math.fsum(thicknesses) <= 5000.


@pytest.mark.parametrize("limits", [
    {"max_total_thickness_nm": 1.},
    {"max_layer_thickness_nm": 1.},
    {"minimum_nonzero_thickness_nm": 500.},
])
def test_boundary_never_bypasses_thickness_constraints(limits):
    request = broad_request(max_layers=32, **limits)
    events: list[dict[str, object]] = []
    assert list(search.iter_reflector_proposals(request, report=events.append)) == []
    assert any(e.get("reason") == "explicit thickness constraints" and e.get("layer_count") == 32
               for e in events)


def test_no_explicit_layer_limit_retains_only_estimated_counts():
    request = broad_request()
    for epoch in (0, 2):
        proposals = list(search.iter_reflector_proposals(request, epoch=epoch))
        assert proposals
        for proposal in proposals:
            provenance = proposal.provenance
            expected = {provenance["estimated_pairs"] * (multiplier + epoch)
                        for multiplier in {1, 2, 2 * provenance["bandwidth_factor"]}}
            assert provenance["pairs"] in expected
            assert provenance["pairs"] < 256
        assert len(proposals) <= 3 * 78


def test_boundary_generator_cancellation_interrupts_partial_consumption():
    request = broad_request(max_layers=32)
    stopped = False

    class Cancelled(Exception):
        pass

    def check():
        if stopped:
            raise Cancelled

    iterator = search.iter_reflector_proposals(request, cancel_check=check)
    next(iterator)
    stopped = True
    with pytest.raises(Cancelled):
        next(iterator)
