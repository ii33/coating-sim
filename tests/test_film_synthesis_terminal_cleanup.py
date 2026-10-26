"""Real frozen-job champion compression, without scheduler or socket services."""
from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmOptimizationRequest
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from coat_sim.core.film_synthesis_jobs import execute_job, is_synthesis_result
from coat_sim.scheduler.jobs import JobSpec
from coat_sim.ui.film_synthesis_quick_adapter import build_synthesis_request, synthesis_spec
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_needle import _optical

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]
QUARTER_WAVE_NM = 550. / (4. * 1.23)
QUARTER_WAVE_R = ((1.5 - 1.23**2) / (1.5 + 1.23**2))**2


@pytest.fixture
def isolate_champion_cleanup(monkeypatch):
    """Reach the old terminal boundary instead of the earlier audit shortcut.

    These fixtures assert exact cleanup slice ownership/counters. Full hybrid
    integration, including the early audit, remains enabled in the other tests.
    """
    monkeypatch.setattr('coat_sim.core.film_synthesis_feasibility.near_feasible_conditions',
                        lambda *_args: False)


def _problem(tmp_path, *, seed=False, seed_multiple=1., alternate_set=False, budget=12):
    service, library, design = fixture(tmp_path)
    if alternate_set:
        film = library.read("acceptance-film")["record"]
        film["optical_constants"].append(_optical("film-set-alternate", 1.23))
        library.write(film, update=True)
    design["front_layers"] = []
    design["substrate"]["sides"] = "single"
    objective = FilmOptimizationObjective(550., 550., 1, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=0., upper_bound=.001), metric="R")
    base = FilmOptimizationRequest(design, 19, (), (objective,), acceptable_normalized_error=0.)
    request = build_synthesis_request(base,
        (FilmNeedleMaterialReference("acceptance-film", "film-set"),),
        {"max_cycles": 1, "max_evaluations": budget, "max_layer_thickness_nm": 200.,
         # This module isolates exact cleanup counters from optional cost search.
         "enable_thickness_reduction": False},
        use_current_seed=False, cpu_workers=1)
    if seed:
        starting = deepcopy(design)
        starting["front_layers"] = [{
            "id": f"seed-{index}", "material_id": "acceptance-film",
            "optical_set_id": "film-set-alternate" if alternate_set and index else "film-set",
            "thickness": {"value": seed_multiple * QUARTER_WAVE_NM / 2., "unit": "nm"},
        } for index in range(2)]
        request = replace(request, seed_designs=(starting,))
    return service, library, freeze_film_synthesis_request(service, request)


def _execute(tmp_path, service, request, *, cancel=None, observer=None):
    spec = JobSpec.parse_frozen(synthesis_spec(service, request)).to_spec()
    assert spec.executor == "film_synthesis"
    assert spec.parameters["payload"]["request"]["request"]["search_strategy"] == "hybrid"
    before = deepcopy(spec.parameters)
    progress, checkpoints = [], []

    def report(item, **_kwargs):
        progress.append(deepcopy(item))
        if observer is not None:
            observer(item)

    response = execute_job({"payload": spec.parameters["payload"]},
        Event() if cancel is None else cancel, report, tmp_path / "best-candidate.json",
        lambda _path, item: checkpoints.append(deepcopy(item)), str(tmp_path.resolve()))
    assert spec.parameters == before
    assert is_synthesis_result(response)
    return response, progress, checkpoints


def _assert_one_scored_and_audited_layer(response, checkpoints):
    result = response["result"]
    candidate = result["candidate_design"]
    assert result["state"] == "completed" and result["targets_met"]
    assert response["best_candidate_audit"]["targets_met"] is True
    assert len(candidate["front_layers"]) == 1
    row = candidate["front_layers"][0]
    assert (row["material_id"], row["optical_set_id"]) == ("acceptance-film", "film-set")
    assert row["thickness"]["value"] == pytest.approx(QUARTER_WAVE_NM, rel=1e-12)
    for key in ("training_score", "validation_score"):
        assert result[key]["design"] == candidate
        assert result[key]["max_error"] == 0.
        assert result[key]["errors"][0]["actual"] == pytest.approx(QUARTER_WAVE_R, abs=1e-14)
    assert result["validation_complete"] is True
    assert checkpoints[-1]["design"] == candidate
    assert checkpoints[-1]["training_score"]["design"] == candidate
    assert checkpoints[-1]["validation_score"]["design"] == candidate
    assert response["review"]["layers"][0]["thickness_nm"] == row["thickness"]["value"]


def test_twelve_evaluation_job_compresses_lagging_real_champion_not_explorer(tmp_path, isolate_champion_cleanup):
    service, library, request = _problem(tmp_path)
    frozen_before, materials_before = deepcopy(request), library._records()
    response, progress, checkpoints = _execute(tmp_path, service, request)
    _assert_one_scored_and_audited_layer(response, checkpoints)
    result = response["result"]
    assert result["evaluations"] == 11 <= request.max_evaluations
    details = result["performance"]["search"]["hybrid"]
    record = next(row for row in details["champion_cleanup"] if not row["terminal"])
    assert (record["source_layer_count"], record["proposed_layer_count"]) == (2, 1)
    assert record["status"] == "complete" and record["evaluations_used"] == 1
    assert record["full_original_spectral_equivalence"] is True
    assert record["independent_audit_performed"] is False
    assert record["promoted_to_strict_champion"] is True
    assert record["selected_physical_hash"] == record["result_physical_hash"]
    # Growth finished scoring its champion before its local slice expired;
    # compression must not reset the separate, still half-wave explorer.
    endpoint = details["exploration_endpoints"][0]
    assert endpoint["design"]["front_layers"][0]["thickness"]["value"] == pytest.approx(
        QUARTER_WAVE_NM / 2.)
    assert endpoint["max_error"] > 0. and endpoint["strict_champion_max_error"] == 0.
    cleanup = next(row for row in progress if row["stage"] == "hybrid_cleanup")
    assert (cleanup["evaluations"], cleanup["layer_count"]) == (7, 1)
    assert all(row["evaluations_used"] <= row["admitted_evaluation_quantum"]
               for row in details["stages"])
    assert request == frozen_before and library._records() == materials_before
    assert request.environment["front_layers"] == [] and request.seed_designs == ()


def test_terminal_seed_champion_is_rescored_then_independently_audited(tmp_path):
    service, _library, request = _problem(tmp_path, seed=True)
    response, _progress, checkpoints = _execute(tmp_path, service, request)
    _assert_one_scored_and_audited_layer(response, checkpoints)
    result = response["result"]
    assert result["evaluations"] == 5
    details = result["performance"]["search"]["hybrid"]
    record = next(row for row in details["champion_cleanup"] if row["terminal"])
    assert record["status"] == "complete" and record["evaluations_used"] == 1
    assert record["independent_audit_performed"] is False
    assert record["promoted_to_strict_champion"] is True
    assert record["selected_physical_hash"] == record["result_physical_hash"]
    terminal = next(row for row in details["stages"] if row["stage"] == "terminal_champion_cleanup")
    assert terminal["admitted_evaluation_quantum"] <= 2
    assert terminal["evaluations_used"] == 1
    assert result["validation_evaluations"] == 2


def test_terminal_merge_cannot_violate_explicit_two_hundred_nm_layer_cap(tmp_path):
    service, _library, request = _problem(tmp_path, seed=True, seed_multiple=3.)
    response, progress, _checkpoints = _execute(tmp_path, service, request)
    result = response["result"]
    assert result["state"] == "completed" and result["targets_met"]
    rows = result["candidate_design"]["front_layers"]
    assert len(rows) == 2 and all(row["thickness"]["value"] < 200. for row in rows)
    assert sum(row["thickness"]["value"] for row in rows) > 200.
    record = result["performance"]["search"]["hybrid"]["terminal_champion_cleanup"]
    assert record["status"] == "skipped_physical_constraints"
    assert record["evaluations_used"] == 0 and result["evaluations"] == 3
    assert not any(row["stage"] == "hybrid_cleanup" for row in progress)


def test_distinct_optical_sets_are_not_merged_or_evaluated_again(tmp_path):
    service, _library, request = _problem(tmp_path, seed=True, alternate_set=True)
    response, progress, _checkpoints = _execute(tmp_path, service, request)
    result = response["result"]
    assert result["state"] == "completed" and result["targets_met"]
    rows = result["candidate_design"]["front_layers"]
    assert [row["optical_set_id"] for row in rows] == ["film-set", "film-set-alternate"]
    details = result["performance"]["search"]["hybrid"]
    assert details["terminal_champion_cleanup"]["status"] == "not_needed"
    assert result["evaluations"] == 3
    assert not any(row["stage"] == "terminal_champion_cleanup" for row in details["stages"])
    assert not any(row["stage"] == "hybrid_cleanup" for row in progress)


def test_exhausted_training_budget_preserves_last_slot_for_real_audit(tmp_path, isolate_champion_cleanup):
    service, _library, request = _problem(tmp_path, seed=True, budget=4)
    response, progress, _checkpoints = _execute(tmp_path, service, request)
    result = response["result"]
    assert result["state"] == "completed" and result["targets_met"]
    assert len(result["candidate_design"]["front_layers"]) == 2
    assert result["evaluations"] == 4 and result["validation_evaluations"] == 1
    record = result["performance"]["search"]["hybrid"]["terminal_champion_cleanup"]
    assert record["status"] == "skipped_evaluation_limit" and record["evaluations_used"] == 0
    assert not any(row["stage"] == "hybrid_cleanup" for row in progress)


@pytest.mark.parametrize(("seed", "stop_stage", "evaluations"), [
    (False, "hybrid_exploration_terminal", 6), (True, "structure_proposal", 3),
])
def test_cancellation_never_adds_champion_cleanup_work(tmp_path, seed, stop_stage, evaluations, isolate_champion_cleanup):
    service, _library, request = _problem(tmp_path, seed=seed)
    cancel = Event()
    response, progress, _checkpoints = _execute(tmp_path, service, request, cancel=cancel,
        observer=lambda row: cancel.set() if row["stage"] == stop_stage else None)
    result = response["result"]
    assert cancel.is_set() and result["state"] == "cancelled"
    assert result["targets_met"] is False and result["evaluations"] == evaluations
    assert len(result["candidate_design"]["front_layers"]) == 2
    assert not any(row["stage"] == "hybrid_cleanup" for row in progress)


def test_integrated_early_audit_still_scores_and_audits_terminal_cleanup(tmp_path):
    service, _library, request = _problem(tmp_path)
    response, progress, checkpoints = _execute(tmp_path, service, request)
    _assert_one_scored_and_audited_layer(response, checkpoints)
    result = response['result']
    assert result['evaluations'] <= request.max_evaluations
    validation_rows = [row for row in progress if row['stage'] == 'validation_complete']
    assert [row['layer_count'] for row in validation_rows] == [2, 1]
    assert not any(row['stage'] == 'hybrid_exploration_terminal' for row in progress)
    details = result['performance']['search']['hybrid']
    assert not any(row['stage'] == 'full_original_slsqp_minimax' for row in details['stages'])
    record = details['terminal_champion_cleanup']
    assert record['status'] == 'complete' and record['evaluations_used'] == 1
    assert record['full_original_spectral_equivalence'] is True
    assert record['selected_physical_hash'] == record['result_physical_hash']
    assert result['validation_evaluations'] == 2


def test_integrated_early_audit_uses_the_last_shared_slot_without_cleanup(tmp_path):
    service, _library, request = _problem(tmp_path, seed=True, budget=3)
    response, progress, _checkpoints = _execute(tmp_path, service, request)
    result = response['result']
    assert result['state'] == 'completed' and result['targets_met']
    assert result['evaluations'] == request.max_evaluations == 3
    assert result['validation_evaluations'] == 1 and result['validation_complete']
    assert result['audit_physical_evaluations'] > 0
    assert result['validation_score']['design'] == result['candidate_design']
    assert len(result['candidate_design']['front_layers']) == 2
    record = result['performance']['search']['hybrid']['terminal_champion_cleanup']
    assert record['status'] == 'skipped_evaluation_limit' and record['evaluations_used'] == 0
    assert not any(row['stage'] == 'hybrid_cleanup' for row in progress)


def test_integrated_cancel_after_early_audit_keeps_exact_audited_design_without_cleanup(tmp_path):
    service, _library, request = _problem(tmp_path)
    cancel = Event()
    response, progress, _checkpoints = _execute(tmp_path, service, request, cancel=cancel,
        observer=lambda row: cancel.set() if row['stage'] == 'validation_complete' else None)
    result = response['result']
    assert cancel.is_set() and result['state'] == 'cancelled' and not result['targets_met']
    at = next(i for i, row in enumerate(progress) if row['stage'] == 'validation_complete')
    assert all(row['evaluations'] == progress[at]['evaluations'] for row in progress[at + 1:])
    assert result['validation_complete'] and result['candidate_provenance']['validation_matches_candidate']
    assert result['validation_score']['design'] == result['candidate_design']
    assert len(result['candidate_design']['front_layers']) == 2
    assert not any(row['stage'] in {'hybrid_cleanup', 'thickness_reduction_trial'} for row in progress[at + 1:])


def test_champion_cleanup_observer_failure_keeps_original_exception(tmp_path):
    service, _library, request = _problem(tmp_path)
    failure = RuntimeError("observer failed after actual champion compression")

    def observer(row):
        if row["stage"] == "hybrid_cleanup":
            raise failure

    with pytest.raises(RuntimeError) as caught:
        _execute(tmp_path, service, request, observer=observer)
    assert caught.value is failure


def test_terminal_resource_guard_does_not_spend_evaluation_or_relabel_parent_audit(tmp_path):
    service, _library, request = _problem(tmp_path, seed=True)
    request = freeze_film_synthesis_request(service, replace(request,
        max_working_set_bytes=128 * 1024**2 + 24000, resource_guard_resolution=None))
    response, progress, _checkpoints = _execute(tmp_path, service, request)
    result = response["result"]
    assert result["state"] == "completed" and result["targets_met"]
    assert len(result["candidate_design"]["front_layers"]) == 2
    assert result["evaluations"] == 3 and result["validation_evaluations"] == 1
    record = result["performance"]["search"]["hybrid"]["terminal_champion_cleanup"]
    assert record["status"] == "skipped_resource_guard"
    assert record["evaluations_used"] == 0
    assert record["promoted_to_strict_champion"] is False
    assert record["independent_audit_performed"] is False
    assert not any(row["stage"] == "hybrid_cleanup" for row in progress)
    assert result["validation_score"]["design"] == result["candidate_design"]


def test_wall_deadline_never_admits_terminal_champion_cleanup(tmp_path, monkeypatch):
    service, _library, request = _problem(tmp_path, seed=True)
    request = replace(request, max_seconds=100.)
    clock = [0.]
    monkeypatch.setattr("coat_sim.core.film_auto_synthesis.monotonic", lambda: clock[0])

    def expire_after_real_seed(item):
        if item["stage"] == "structure_proposal":
            clock[0] = 101.

    response, progress, _checkpoints = _execute(tmp_path, service, request,
        observer=expire_after_real_seed)
    result = response["result"]
    assert result["state"] == "not_found" and result["termination_reason"] == "time_limit"
    assert result["targets_met"] is False
    assert result["evaluations"] == 3 and result["validation_evaluations"] == 1
    assert len(result["candidate_design"]["front_layers"]) == 2
    assert not any(row["stage"] == "hybrid_cleanup" for row in progress)
    assert not any(row["stage"] == "terminal_champion_cleanup"
                   for row in result["performance"]["search"]["hybrid"]["stages"])
