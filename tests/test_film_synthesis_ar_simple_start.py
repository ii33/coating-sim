"""Simple AR audit exit and complex-family fallback share production budgets."""
from dataclasses import replace
from threading import Event

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_ar_clusters as clusters
from coat_sim.core import film_synthesis_matching as matching
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def test_legal_single_layer_passes_full_audit_before_complex_preparation(monkeypatch):
    service, base = setup_problem(upper=.001)
    request = replace(base, ar_structure_mode="clustered", max_evaluations=3,
                      cpu_workers=2)
    def unnecessary(*args, **kwargs):
        raise AssertionError("An audited simple solution must exit before complex preparation")
    monkeypatch.setattr(matching, "iter_matching_proposals", unnecessary)
    monkeypatch.setattr(clusters, "iter_ar_cluster_proposals", unnecessary)
    result = synthesis.synthesize_film_design(service, request)
    assert result.targets_met and result.state == "completed"
    assert result.validation_complete and result.validation_evaluations == 1
    assert result.evaluations == 3 and result.cycles == 0
    assert result.candidate_design is not None
    layers = result.candidate_design["front_layers"]
    assert len(layers) == 1 and layers[0]["material_id"] == "low"
    assert layers[0]["thickness"]["value"] > 0
    assert result.training_score is not None and result.validation_score is not None
    assert result.training_score["max_error"] == 0
    assert result.validation_score["max_error"] == 0
    assert len(result.validation_score["errors"]) > len(result.training_score["errors"])
    assert result.performance["search"]["ar_simple_start"]["audited_pass"]
    assert not result.performance["search"]["global_search"]
    assert result.performance["submitted_tasks"] == 0
    assert result.performance["worker_pids"] == []
    assert base.environment["front_layers"] == []


def test_unmet_objective_retains_matching_and_cluster_search():
    service, base = setup_problem(upper=.05)
    # The first objective is easy; the second remains impossible over a band.
    request = replace(base, ar_structure_mode="clustered", max_evaluations=50,
        objectives=(*base.objectives, FilmOptimizationObjective(600., 640., 5, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=0.), metric="R")))
    event, stages = Event(), []
    def observe(item):
        stages.append(item.stage)
        if item.stage == "structure_proposal":
            event.set()
    result = synthesis.synthesize_film_design(service, request,
        cancel_event=event, progress_callback=observe)
    assert not result.targets_met and result.state == "cancelled"
    assert stages.index("ar_simple_start") < stages.index("structure_proposal")
    assert not result.performance["search"]["ar_simple_start"]["audited_pass"]
    assert result.performance["search"]["ar_simple_start"]["feasible"] == 2
    assert result.frozen_request.objectives == request.objectives
    assert result.performance["search"]["cluster_structure_schedule"]["noncluster_control_retained"]
    assert result.performance["cleanup_complete"]


@pytest.mark.parametrize("stop", ["cancel", "time_limit", "observer_failure"])
def test_terminal_search_snapshot_follows_real_family_cleanup(monkeypatch, stop):
    service, base = setup_problem(upper=.05)
    request = replace(base, ar_structure_mode="clustered", max_evaluations=50,
        max_seconds=60., objectives=(*base.objectives,
            FilmOptimizationObjective(600., 640., 5, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=0.), metric="R")))
    generated, closed = {"matching": 0, "cluster": 0}, []
    for name, module, attribute in (("matching", matching, "iter_matching_proposals"),
                                    ("cluster", clusters, "iter_ar_cluster_proposals")):
        original = getattr(module, attribute)
        def track(*args, _name=name, _original=original, **kwargs):
            try:
                for proposal in _original(*args, **kwargs):
                    generated[_name] += 1
                    yield proposal
            finally:
                closed.append(_name)
        monkeypatch.setattr(module, attribute, track)
    clock, event, terminal = [0.], Event(), []
    monkeypatch.setattr(synthesis, "monotonic", lambda: clock[0])
    failure = RuntimeError("structure observer failure")
    def observe(item):
        if item.stage == "structure_proposal":
            if stop == "cancel":
                event.set()
            elif stop == "time_limit":
                clock[0] = 61.
            else:
                raise failure
        elif item.stage in ("cancelled", "not_found"):
            # Completion observers must see released streams, not deferred
            # cleanup that only runs after the result snapshot was copied.
            terminal.append(item.stage)
            assert sorted(closed) == ["cluster", "matching"]
    if stop == "observer_failure":
        with pytest.raises(RuntimeError) as caught:
            synthesis.synthesize_film_design(service, request, cancel_event=event, progress_callback=observe)
        assert caught.value is failure
    else:
        result = synthesis.synthesize_film_design(service, request, cancel_event=event, progress_callback=observe)
        assert result.termination_reason == ("cancelled" if stop == "cancel" else "time_limit")
        assert terminal == [result.state]
        assert result.evaluations == 4 and result.validation_evaluations == 0
        assert not result.targets_met and not result.validation_complete
        assert result.candidate_provenance["training_complete"]
        assert result.performance["cleanup_complete"]
        search = result.performance["search"]
        for name in generated:
            schedule = search[name + "_structure_schedule"]
            assert schedule["latest_consumed"] == generated[name] == 1
            assert schedule["pending"]
        assert search["cluster_structure_schedule"]["noncluster_control_retained"]
    assert generated == {"matching": 1, "cluster": 1}
    assert sorted(closed) == ["cluster", "matching"]


def test_simple_prefix_obeys_physical_thickness_and_cancellation():
    service, base = setup_problem(upper=0.)
    request = replace(base, ar_structure_mode="clustered",
        minimum_nonzero_thickness_nm=150., max_layer_thickness_nm=160.,
        max_total_thickness_nm=155.)
    event = Event()
    def observe(item):
        if item.stage == "ar_simple_start":
            event.set()
    result = synthesis.synthesize_film_design(service, request,
        cancel_event=event, progress_callback=observe)
    assert result.state == "cancelled" and not result.targets_met
    assert result.candidate_design is not None
    ds = [row["thickness"]["value"] for row in result.candidate_design["front_layers"]]
    assert all(150. <= d <= 160. for d in ds) and sum(ds) <= 155.
    assert not result.performance["search"]["global_search"]
    assert result.performance["cleanup_complete"]


def test_exhausted_training_budget_admits_no_simple_or_complex_proposal():
    service, base = setup_problem(upper=0.)
    result = synthesis.synthesize_film_design(service,
        replace(base, ar_structure_mode="clustered", max_evaluations=2))
    assert result.termination_reason == "evaluation_limit"
    assert result.evaluations <= 2 and not result.targets_met
    assert not any(row.stage == "ar_simple_start" for row in result.trace)
    assert not result.performance["search"]["structural_proposals"]


def test_default_gui_hybrid_typed_job_keeps_three_evaluation_audited_quarter_wave_exit(tmp_path):
    from coat_sim.core.film_optimization import FilmOptimizationRequest
    from coat_sim.core.film_synthesis_jobs import deserialize_job_payload, execute_job
    from coat_sim.scheduler.jobs import JobSpec
    from coat_sim.ui.film_synthesis_quick_adapter import (
        SYNTHESIS_DEFAULT_OPTIONS, build_synthesis_request, synthesis_spec,
    )
    service, fixture = setup_problem(upper=.001)
    base = FilmOptimizationRequest(fixture.environment, 82, (), fixture.objectives,
        acceptable_normalized_error=0., validation_samples_multiplier=4)
    options = {key: getattr(fixture, key) for key in SYNTHESIS_DEFAULT_OPTIONS}
    options.update(max_evaluations=3)
    request = build_synthesis_request(base, fixture.candidate_materials, options,
        use_current_seed=False, cpu_workers=2)
    assert request.ar_structure_mode == "clustered" and request.seed_designs == ()
    assert request.search_strategy == "hybrid"
    assert request.growth_quarter_wave_fractions == (.5, 1., 2.)
    frozen = synthesis.freeze_film_synthesis_request(service, request)
    job = JobSpec.parse_frozen(synthesis_spec(service, frozen)).to_spec()
    _, decoded, structures = deserialize_job_payload(job.parameters["payload"])
    assert decoded == frozen and decoded.max_evaluations == 3 and not structures
    assert decoded.objectives == fixture.objectives
    response = execute_job({"payload": job.parameters["payload"]}, Event(),
        lambda *_args, **_kwargs: None, tmp_path / "best.json",
        lambda _path, _value: None, str(tmp_path.resolve()))
    result = response["result"]
    assert result["state"] == "completed" and result["targets_met"]
    assert result["validation_complete"] and result["evaluations"] == 3
    assert result["validation_evaluations"] == 1
    assert result["training_score"]["max_error"] == result["validation_score"]["max_error"] == 0
    assert len(result["validation_score"]["errors"]) > len(result["training_score"]["errors"])
    layers = result["candidate_design"]["front_layers"]
    assert len(layers) == 1 and layers[0]["material_id"] == "low"
    assert layers[0]["thickness"]["value"] > 0
    search = result["performance"]["search"]
    assert "ar_simple_start" not in search
    assert [row["stage"] for row in search["hybrid"]["stages"]] == ["engineering_prior_screen"]
    assert not search["hybrid"]["needle_scans"] and not search["global_search"]
    archive = search["design_archive"]
    audited = next(entry for entry in archive["entries"]
                   if entry["physical_hash"] == archive["audited_champion_hash"])
    assert audited["provenance"] == {"family": "homogeneous", "quarter_fraction": 1.0}
    assert audited["audit_status"] == "complete" and audited["audit"]["max_error"] == 0
    assert audited["design"] == result["candidate_design"]
    assert response["best_candidate_audit"]["available"]
    assert response["best_candidate_audit"]["targets_met"]
    assert result["performance"]["submitted_tasks"] == 0
    assert result["performance"]["worker_pids"] == []
    assert result["performance"]["cleanup_complete"]
    assert fixture.environment["front_layers"] == []
