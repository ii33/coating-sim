from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from threading import Event

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_optimization import FilmOptimizationRequest, FilmOptimizationObjective
from coat_sim.core.film_synthesis_jobs import deserialize_job_payload, execute_job
from coat_sim.scheduler.jobs import JobSpec
from coat_sim.ui.film_synthesis_quick_adapter import (
    SYNTHESIS_DEFAULT_OPTIONS, build_synthesis_request, synthesis_spec,
)
from tests.test_film_auto_synthesis import setup_problem


def test_gui_default_request_survives_freeze_job_roundtrip_and_executes_global_structure(
        tmp_path):
    service, fixture_request = setup_problem(materials=("low", "high"))
    objective = FilmOptimizationObjective(
        540., 560., 3, angles_deg=(40., 50.), polarization="unpolarized",
        metric="R", mode="range", lower_bound=.9,
    )
    base = FilmOptimizationRequest(
        fixture_request.environment, design_revision=73, variables=(), objectives=(objective,))

    # Mirrors the GUI's default path: no current-stack seed, blank physical caps,
    # and the requested 19 inner workers.
    request = build_synthesis_request(
        base, fixture_request.candidate_materials,
        {key: None for key in SYNTHESIS_DEFAULT_OPTIONS},
        use_current_seed=False, cpu_workers=19,
    )
    assert request.seed_designs == ()
    assert request.cpu_workers == 19
    assert all(getattr(request, name) is None for name in SYNTHESIS_DEFAULT_OPTIONS)
    assert request.objectives == (objective,)
    assert request.candidate_materials == fixture_request.candidate_materials

    # The real controller freezes the material snapshot before synthesis_spec.
    frozen_request = freeze_film_synthesis_request(service, request)
    raw_spec = synthesis_spec(service, frozen_request)
    frozen_spec = JobSpec.parse_frozen(raw_spec).to_spec()
    assert frozen_spec.executor == "film_synthesis"
    job = frozen_spec.parameters["payload"]
    request_id, decoded, physical_structures = deserialize_job_payload(job)
    assert request_id
    assert physical_structures == []
    assert decoded.seed_designs == ()
    assert decoded.cpu_workers == 19
    assert decoded.objectives == (objective,)
    assert decoded.candidate_materials == fixture_request.candidate_materials
    assert all(getattr(decoded, name) is None for name in SYNTHESIS_DEFAULT_OPTIONS)
    assert "use_current_seed" not in job["request"]
    assert not job["request"].get("seed_designs")
    legacy_job = deepcopy(job)
    legacy_request = legacy_job["request"]["request"]
    assert "structure_search_seed" in legacy_request
    legacy_request.pop("structure_search_seed")
    _, legacy_decoded, _ = deserialize_job_payload(legacy_job)
    assert legacy_decoded.structure_search_seed == 0
    assert legacy_decoded.seed_designs == ()

    cancel = Event()
    progress = []

    def on_progress(item, **_kwargs):
        progress.append(item)

    result = execute_job(
        {"payload": job}, cancel, on_progress, tmp_path / "best-candidate.json",
        lambda _path, _value: None, str(tmp_path.resolve()),
    )
    assert result["result"]["state"] == "completed"
    assert result["result"]["targets_met"] is True
    assert result["result"]["cycles"] == 0
    assert result["result"]["candidate_design"] is not None
    assert len(result["result"]["candidate_design"]["front_layers"]) > 1
    assert result["result"]["validation_score"]["max_error"] <= .001
    assert result["best_candidate_audit"]["targets_met"] is True
    assert result["result"]["frozen_request"]["cpu_workers"] == 19
    generated = [item for item in progress if item.get("stage") == "structure_proposal"]
    assert generated and all(item["layer_count"] > 1 for item in generated)


def test_gui_v23_matching_mode_recovers_after_training_pass_but_dense_audit_failure(tmp_path):
    service, fixture = setup_problem(upper=.05, low=450., high=700., samples=2)
    service.materials.records['glass']['optical_sets'][0]['n'] = [1.5, 3., 1.5]
    base = FilmOptimizationRequest(fixture.environment, 74, (), fixture.objectives,
        acceptable_normalized_error=fixture.acceptable_normalized_error,
        validation_samples_multiplier=fixture.validation_samples_multiplier)
    request = build_synthesis_request(base, fixture.candidate_materials,
        {key: getattr(fixture, key) for key in SYNTHESIS_DEFAULT_OPTIONS},
        use_current_seed=False, cpu_workers=1)
    request = replace(request, ar_structure_mode="v23_matching")
    frozen = freeze_film_synthesis_request(service, request)
    from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate
    bare_audit = audit_frozen_synthesis_candidate(frozen, frozen.environment)
    assert bare_audit['score']['max_error'] > .15
    job = JobSpec.parse_frozen(synthesis_spec(service, frozen)).to_spec()
    _, decoded, structures = deserialize_job_payload(job.parameters['payload'])
    assert not structures and decoded.seed_designs == ()
    assert decoded.objectives == fixture.objectives
    progress = []
    response = execute_job({'payload': job.parameters['payload']}, Event(),
        lambda item, **_kwargs: progress.append(item), tmp_path / 'ar-best.json',
        lambda _path, _value: None, str(tmp_path.resolve()))
    result = response['result']
    assert result['state'] == 'completed' and result['targets_met']
    assert result['cycles'] > 0
    assert response['best_candidate_audit']['targets_met'] is True
    assert result['training_score']['max_error'] <= request.acceptable_normalized_error
    assert result['validation_score']['max_error'] <= request.acceptable_normalized_error
    assert len(result['validation_score']['errors']) >= 9
    assert result['validation_score']['audit']['dense_points'] == 5
    assert any(row.get('stage') == 'structure_proposal' and '"family": "matching"' in row.get('status', '')
               for row in progress)
    assert result['performance']['cleanup_complete']
    assert result['performance']['unknown_tasks'] == 0
