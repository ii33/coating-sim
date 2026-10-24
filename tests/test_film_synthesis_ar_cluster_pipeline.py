"""Default GUI/durable cluster integration, using real bounded core work."""
from dataclasses import replace
from threading import Event

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload, film_synthesis_request_from_payload
from coat_sim.core.film_synthesis_jobs import deserialize_job_payload, execute_job
from coat_sim.core.film_optimization import FilmOptimizationRequest
from coat_sim.scheduler.jobs import JobSpec
from coat_sim.ui.film_synthesis_quick_adapter import build_synthesis_request, synthesis_spec, SYNTHESIS_DEFAULT_OPTIONS
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


@pytest.mark.parametrize('mode', [None, True, 1, '', 'v23', 'continuous_n'])
def test_ar_mode_rejects_invalid_or_unimplemented_models(mode):
    _, request = setup_problem()
    with pytest.raises(ValueError, match='ar_structure_mode'):
        synthesis._validated(replace(request, ar_structure_mode=mode))


def test_mode_payload_roundtrip_rejects_missing_current_field():
    service, request = setup_problem()
    frozen = synthesis.freeze_film_synthesis_request(service, request)
    payload = film_synthesis_request_to_payload('ar-mode-test', frozen)
    assert film_synthesis_request_from_payload(payload)[1].ar_structure_mode == 'v23_matching'
    payload['request'].pop('ar_structure_mode')
    with pytest.raises(ValueError, match='fields'):
        film_synthesis_request_from_payload(payload)


def test_explicit_legacy_gui_cluster_route_runs_global_before_gradient_and_cancels_cleanly(tmp_path):
    service, fixture = setup_problem(upper=0., samples=3)
    base = FilmOptimizationRequest(fixture.environment, 82, (), fixture.objectives,
        acceptable_normalized_error=0., validation_samples_multiplier=4)
    options = {key: getattr(fixture, key) for key in SYNTHESIS_DEFAULT_OPTIONS}
    # This ordering belongs to the retained legacy topology optimizer, not to
    # the new staged hybrid default. Keep its durable route explicitly covered.
    options.update(max_evaluations=1400, max_layers=12, search_strategy="legacy")
    request = build_synthesis_request(base, fixture.candidate_materials, options,
        use_current_seed=False, cpu_workers=1)
    assert request.ar_structure_mode == 'clustered' and request.seed_designs == ()
    assert request.search_strategy == "legacy"
    frozen = synthesis.freeze_film_synthesis_request(service, request)
    job = JobSpec.parse_frozen(synthesis_spec(service, frozen)).to_spec()
    _, decoded, structures = deserialize_job_payload(job.parameters['payload'])
    assert decoded.ar_structure_mode == 'clustered' and not structures
    assert decoded.search_strategy == "legacy"
    assert decoded.objectives == fixture.objectives
    event, progress = Event(), []
    def observe(item, **_kwargs):
        progress.append(item)
        if item['stage'] == 'interior_local_trial':
            event.set()
    response = execute_job({'payload': job.parameters['payload']}, event, observe,
        tmp_path / 'best.json', lambda _p, _v: None, str(tmp_path.resolve()))
    result = response['result']
    stages = [p['stage'] for p in progress]
    assert 'structure_global_generation' in stages
    assert stages.index('structure_global_generation') < stages.index('interior_local_trial')
    assert 'structural_local_refinement' not in stages and 'needle_probe' not in stages
    search = result['performance']['search']
    proposals = search['structural_proposals']
    assert {p.get('family') for p in proposals} >= {'matching', 'ar_cluster'}
    assert {p['cluster_count'] for p in proposals if p.get('family') == 'ar_cluster' and 'max_error' in p} == {1, 2, 3}
    assert any(g['completed_generations'] == 4 for g in search['global_search'])
    assert search['cluster_structure_schedule']['noncluster_control_retained'] is True
    assert result['state'] == 'cancelled' and not result['targets_met']
    assert result['candidate_design'] is not None and result['training_score'] is not None
    assert result['performance']['cleanup_complete'] and result['performance']['unknown_tasks'] == 0


def test_v23_control_keeps_original_growth_before_structure():
    service, request = setup_problem(upper=0., samples=3)
    event, stages = Event(), []
    def observe(item):
        stages.append(item.stage)
        if item.stage == 'needle_probe':
            event.set()
    result = synthesis.synthesize_film_design(service, request, cancel_event=event, progress_callback=observe)
    assert stages.index('needle_scan') < stages.index('needle_probe')
    assert 'structure_global_generation' not in stages
    assert not any(p.get('family') == 'ar_cluster' for p in result.performance['search']['structural_proposals'])
    assert result.frozen_request.ar_structure_mode == 'v23_matching'
    assert result.state == 'cancelled' and result.performance['cleanup_complete']


def test_default_gui_hybrid_typed_job_cancels_after_real_analytic_probe(tmp_path):
    from coat_sim.core.film_design_service import simulate_frozen

    service, fixture = setup_problem(upper=0., samples=3)
    base = FilmOptimizationRequest(fixture.environment, 82, (), fixture.objectives,
        acceptable_normalized_error=0., validation_samples_multiplier=4)
    options = {key: getattr(fixture, key) for key in SYNTHESIS_DEFAULT_OPTIONS}
    options.update(max_evaluations=120, max_layers=12)
    request = build_synthesis_request(base, fixture.candidate_materials, options,
        use_current_seed=False, cpu_workers=1)
    assert request.search_strategy == "hybrid" and request.ar_structure_mode == "clustered"
    assert not hasattr(request, "objective_weighting")
    assert request.max_working_set_bytes == 0
    assert request.seed_designs == ()
    frozen = synthesis.freeze_film_synthesis_request(service, request)
    job = JobSpec.parse_frozen(synthesis_spec(service, frozen)).to_spec()
    _, decoded, structures = deserialize_job_payload(job.parameters["payload"])
    assert decoded == frozen and not structures
    assert decoded.objectives == fixture.objectives
    event, progress = Event(), []

    def observe(item, **_kwargs):
        progress.append(item)
        if item["stage"] == "needle_probe":
            event.set()

    response = execute_job({"payload": job.parameters["payload"]}, event, observe,
        tmp_path / "best.json", lambda _path, _value: None, str(tmp_path.resolve()))
    result = response["result"]
    stages = [item["stage"] for item in progress]
    assert stages.index("hybrid_rotation") < stages.index("structure_proposal")
    assert stages.index("hybrid_global_trial") < stages.index("needle_scan") < stages.index("needle_probe")
    assert stages[-1] == "cancelled" and "validation" not in stages
    assert any(item["stage"] == "needle_probe" and item.get("sensitivity") is not None
               for item in result["trace"])
    search = result["performance"]["search"]
    hybrid = search["hybrid"]
    assert hybrid["objective_masses"] == pytest.approx([1.])
    assert [row["stage"] for row in hybrid["stages"]] == [
        "engineering_prior_screen", "local_global_exploration", "analytic_needle_scan"]
    assert all(row["evaluations_used"] > 0 for row in hybrid["stages"])
    assert hybrid["resource_guard_resolution"] == decoded.resource_guard_resolution
    assert {entry["provenance"].get("family")
            for entry in search["design_archive"]["entries"]} >= {
                "homogeneous", "matching", "ar_cluster"}
    assert result["state"] == result["termination_reason"] == "cancelled"
    assert not result["targets_met"] and not result["validation_complete"]
    assert result["validation_evaluations"] == 0 and result["validation_score"] is None
    assert not response["best_candidate_audit"]["available"]
    assert result["candidate_provenance"]["training_complete"]
    assert result["candidate_provenance"]["frozen_at_cancel"]
    candidate, score = result["candidate_design"], result["training_score"]
    assert candidate is not None and score is not None
    assert candidate["front_layers"] and all(
        row["thickness"]["value"] > 0 for row in candidate["front_layers"])
    # Recheck the retained training candidate with the direct physical service,
    # independently of the hybrid archive and its cancelled zero-thickness probe.
    physical = service.freeze_design(candidate, dict(
        wavelength_min_nm=520., wavelength_max_nm=580., samples=3))
    direct = simulate_frozen(".", physical)
    assert max(direct["R"]) == pytest.approx(score["max_error"])
    assert score["max_error"] < .04
    performance = result["performance"]
    assert performance["cleanup_complete"] and performance["unknown_tasks"] == 0
    assert performance["active_worker_pids"] == []
    assert fixture.environment["front_layers"] == []
