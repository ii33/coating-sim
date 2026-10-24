from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_global as global_search
from coat_sim.core import film_synthesis_parallel as parallel
from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest, freeze_film_synthesis_request
from coat_sim.core.film_design import new_design
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from coat_sim.core.refractiveindex_local import RefractiveIndexLocalStore, packaged_store_root
from tests.test_film_auto_synthesis import Service, setup_problem

pytestmark = pytest.mark.algorithm


def real_reflector_request(**limits):
    store = RefractiveIndexLocalStore(packaged_store_root())
    records = []
    for material, source in (("MgF2", "main/MgF2/nk/Rodriguez-de Marcos.yml"),
                             ("ZnS", "main/ZnS/nk/Amotchkina.yml")):
        entry = next(row for row in store.search(material, limit=100)["entries"]
                     if row["source_path"] == source)
        optical = store.convert_entry(entry["entry_id"], optical_set_id="nk",
                                      name=material)["optical_set"]
        records.append(dict(id=material, name=material, design_ready=True, optical_sets=[optical]))
    service = Service(records)
    environment = new_design(design_id="global-reflector-test")
    # The pinned MgF2 n-only source is admitted only by this explicit opt-in.
    environment["substrate"].update(material_id="MgF2", optical_set_id="nk")
    environment["assume_lossless"] = True
    request = FilmSynthesisRequest(
        environment,
        (FilmOptimizationObjective(600, 900, 4, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=.99, upper_bound=None), angles_deg=(40., 50.), polarization="unpolarized", metric="R"),),
        (FilmNeedleMaterialReference("ZnS", "nk"), FilmNeedleMaterialReference("MgF2", "nk")),
        **limits,
    )
    return service, freeze_film_synthesis_request(service, request)


def test_real_nk_oblique_reflector_proposals_use_multilayer_structure():
    _, request = real_reflector_request()
    proposals = list(global_search.iter_reflector_proposals(request))
    assert proposals
    assert all(len(item.design["front_layers"]) > 1 for item in proposals)
    assert all(item.provenance["estimated_pairs"] > 1 for item in proposals)
    assert {tuple(sorted((p.provenance["high"][0], p.provenance["low"][0])))
            for p in proposals} == {("MgF2", "ZnS")}
    assert all(p.provenance["phase_angle_deg"] == 45. for p in proposals)
    assert any(len(set(layer["material_id"] for layer in p.design["front_layers"])) == 2
               for p in proposals)


def test_reflector_prior_retains_combined_mean_and_pointwise_candidates():
    _, pointwise = real_reflector_request()
    both = replace(pointwise, objectives=tuple(replace(o, mean=replace(o.pointwise))
        for o in pointwise.objectives))

    pointwise_output = list(global_search.iter_reflector_proposals(pointwise))
    both_output = list(global_search.iter_reflector_proposals(both))
    assert len(pointwise_output) == len(both_output) == 234
    assert both_output[:12] == pointwise_output[:12]
    assert both_output == pointwise_output
    selected = global_search.reflector_objectives(both)
    assert selected == both.objectives and selected[0] is both.objectives[0]
    assert selected[0].pointwise.enabled and selected[0].mean.enabled

    mean_only = replace(both, objectives=tuple(replace(o,
        pointwise=replace(o.pointwise, enabled=False)) for o in both.objectives))
    assert global_search.reflector_objectives(mean_only) == ()
    assert list(global_search.iter_reflector_proposals(mean_only)) == []


@pytest.mark.parametrize("limit", [
    {"max_layers": 1},
    {"max_layer_thickness_nm": 1.0},
    {"max_total_thickness_nm": 1.0},
])
def test_structural_proposals_never_bypass_explicit_manufacturing_limits(limit):
    _, request = real_reflector_request(**limit)
    events: list[dict[str, object]] = []
    proposals = list(global_search.iter_reflector_proposals(request, report=events.append))
    assert proposals == []
    assert events and all(event["stage"] == "structure_skipped" for event in events)


def test_non_high_reflectance_objective_does_not_activate_reflector_prior():
    _, request = setup_problem()
    request = replace(request, objectives=(replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, upper_bound=.2)),))
    assert global_search.reflector_objectives(request) == ()
    assert list(global_search.iter_reflector_proposals(request)) == []


def test_unknown_k_requires_explicit_lossless_opt_in():
    service, request = setup_problem()
    service.materials.records["high"]["optical_sets"][0].pop("k")
    with pytest.raises(ValueError, match="k|absorption|unknown"):
        freeze_film_synthesis_request(service, request)


def test_de_is_seeded_mutates_and_improves_a_multimodal_loss():
    initial = np.array([2.7, -2.4])
    lower, upper = np.full(2, -3.), np.full(2, 3.)
    first = global_search.DifferentialEvolutionProposals(
        initial, lower, upper, seed=17, population_size=20)
    replay = global_search.DifferentialEvolutionProposals(
        initial, lower, upper, seed=17, population_size=20)

    def loss(points):
        # Repeating wells create a small multimodal search landscape.
        return np.sum(points ** 2 + .35 * (1. - np.cos(5. * points)), axis=1)

    initial_points = first.proposals()
    np.testing.assert_array_equal(initial_points, replay.proposals())
    initial_losses = loss(initial_points)
    first.accept(initial_losses)
    replay.accept(initial_losses)
    changed_points = first.proposals()
    np.testing.assert_array_equal(changed_points, replay.proposals())
    assert np.any(changed_points != initial_points)
    first.accept(loss(changed_points))
    assert first.generations == 1
    assert first.losses is not None
    assert float(np.min(first.losses)) < float(np.min(initial_losses))


def test_global_batch_with_19_requested_workers_caps_pool_and_preserves_order(monkeypatch):
    monkeypatch.setattr(parallel, "MIN_PARALLEL_WORK", 0)
    monkeypatch.setattr(parallel, "available_cpu_workers", lambda: 19)
    service, request = setup_problem()
    frozen = synthesis.freeze_film_synthesis_request(service, request)
    executor = parallel.OrderedEvaluationExecutor(19)
    executor.configure(synthesis._proxy(frozen))
    assert executor.eligible(3, 200_000, budget_allows=False) is False
    assert executor.selection_reasons["finite_budget_serial"] == 1
    tasks = [parallel.EvaluationTask(frozen.environment, jacobian=False, context=index)
             for index in range(3)]
    received = []
    try:
        with executor.batch(tasks, count=3, work=200_000, budget_allows=True,
                            phase="global") as batch:
            for task, outcome in batch:
                assert outcome is not None
                executor.take(outcome)
                received.append(task.context)
    finally:
        executor.close()
    assert received == [0, 1, 2]
    assert executor.batch_decisions[0]["parallel_selected"]
    assert executor.batch_decisions[0]["candidate_count"] < 2 * executor.requested_workers
    assert executor.requested_workers == 19
    assert executor.diagnostics()["effective_workers"] == 3
    assert executor.diagnostics()["cleanup_complete"]


def test_global_prior_honors_cancellation_before_proposing():
    _, request = real_reflector_request()
    class Cancelled(Exception):
        pass

    def cancel():
        raise Cancelled

    iterator = global_search.iter_reflector_proposals(request, cancel_check=cancel)
    with pytest.raises(Cancelled):
        next(iterator)
