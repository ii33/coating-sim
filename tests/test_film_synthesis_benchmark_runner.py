"""Small production-chain contracts; no fixture services or long benchmarks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from threading import Event

import pytest

from tools import run_film_synthesis_benchmarks as runner
from tools.run_film_synthesis_acceptance import atomic_json

SUITE = runner.ROOT / "benchmarks/film_synthesis/20261021/representative-suite-v2.1.json"


def current_objective(low=550., high=550., samples=1, *, angles=(0.,), lower=None, upper=1., strict_lower=False):
    from coat_sim.core.film_objective_payload import objective_to_payload
    from coat_sim.core.film_optimization import FilmObjectiveConstraint, FilmOptimizationObjective
    return objective_to_payload(FilmOptimizationObjective(low, high, samples,
        angles_deg=angles, polarization="s", metric="R",
        pointwise=FilmObjectiveConstraint(lower_bound=lower, upper_bound=upper, strict_lower=strict_lower)))


def small_suite(tmp_path, **changes):
    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    case = deepcopy(suite["cases"][0])
    case.pop("strict_lower_bound_objective_indices", None)
    case.pop("strict_upper_bound_objective_indices", None)
    case.pop("qualification", None)
    case.update(id="tiny_production_contract", category="current nested contract test",
                max_evaluations=2, wall_seconds=3, objectives=[current_objective()])
    case.update(changes)
    suite["suite_id"] = "current-nested-contract-test"
    suite["success_rule"] = "Current nested mean/pointwise criteria contract test."
    suite["cases"] = [case]
    suite["defaults"]["seeds"] = [0]
    path = tmp_path / "suite.json"
    atomic_json(path, suite)
    return path


def test_preflight_uses_actual_gui_freeze_and_durable_contract(tmp_path):
    compiled = runner.compile_preflight(small_suite(tmp_path), seed=[0, 1], workers=2)
    assert len(compiled["runs"]) == 2
    first, second = compiled["runs"]
    request = first["frozen_request"]
    assert request["environment"]["front_layers"] == request["environment"]["back_layers"] == []
    assert request["seed_designs"] == []
    assert request["cpu_workers"] == 2
    assert request["structure_search_seed"] == 0
    assert second["frozen_request"]["structure_search_seed"] == 1
    assert first["request_sha256"] != second["request_sha256"]
    assert first["job_spec"]["executor"] == "film_synthesis"
    assert request["frozen_material_snapshot"]["materials"]
    assert not (tmp_path / "result.json").exists()


def test_preflight_request_hash_is_stable_despite_new_job_identity(tmp_path, monkeypatch):
    # The frozen AUTO policy deliberately records the memory seen at freeze;
    # hold that input constant while testing unrelated generated job identity.
    monkeypatch.setattr("coat_sim.core.compute_resources.available_memory_bytes", lambda: 8 * 1024**3)
    suite = small_suite(tmp_path)
    first = runner.compile_preflight(suite)
    second = runner.compile_preflight(suite)
    assert first["runs"][0]["request_sha256"] == second["runs"][0]["request_sha256"]
    assert first["source_sha256"] == second["source_sha256"]


@pytest.mark.parametrize("argument,value,message", [
    ("case_id", "absent", "Unknown case"),
    ("seed", True, "Seed"), ("seed", -1, "Seed"),
    ("workers", 0, "workers"), ("workers", True, "workers"),
])
def test_preflight_rejects_invalid_selection(tmp_path, argument, value, message):
    with pytest.raises(ValueError, match=message):
        runner.compile_preflight(small_suite(tmp_path), **{argument: value})


def test_preflight_refuses_changed_catalog_hash(tmp_path):
    path = small_suite(tmp_path)
    suite = json.loads(path.read_text(encoding="utf-8"))
    suite["catalog_sha256"] = "0" * 64
    atomic_json(path, suite)
    with pytest.raises(ValueError, match="catalog_sha256"):
        runner.compile_preflight(path)


def test_output_is_new_and_restricted_to_temp(tmp_path):
    target = tmp_path / "new-run"
    assert runner.reserve_output(target) == target.resolve()
    atomic_json(target / "result.json", {"terminal": True})
    with pytest.raises(FileExistsError):
        runner.reserve_output(target)
    assert json.loads((target / "result.json").read_text()) == {"terminal": True}
    with pytest.raises(ValueError, match="Temp"):
        runner.reserve_output(Path(tempfile.gettempdir()))
    with pytest.raises(ValueError, match="Temp"):
        runner.reserve_output(Path(tempfile.gettempdir()).parent / "not-temp-benchmark-output")


def single_condition(lower, upper, actual):
    from dataclasses import asdict
    from coat_sim.core.film_objective_criteria import reduce_objective_responses
    from coat_sim.core.film_objective_payload import objective_from_payload
    objective = current_objective(600., 600., 1, lower=lower, upper=upper,
                                  strict_lower=lower is not None)
    request = {"objectives": [objective], "acceptable_normalized_error": 0.,
               "validation_samples_multiplier": 1}
    reduced = reduce_objective_responses((objective_from_payload(objective),), [{
        "objective_index": 0, "angle_deg": 0., "wavelength_nm": [600.], "actual_values": [actual]}])
    score = {"errors": [asdict(row) for row in reduced.conditions],
             "coverage": [asdict(row) for row in reduced.coverage],
             "max_error": reduced.max_error, "weighted_max_error": reduced.weighted_max_error,
             "weighted_rms_error": reduced.weighted_rms_error}
    return request, score


def test_raw_acceptance_obeys_original_nested_strict_lower_without_epsilon():
    request, score = single_condition(.99, None, .99)
    assert runner.raw_acceptance(request, score)["passed"] is False
    request, score = single_condition(.99, None, .990000000000001)
    assert runner.raw_acceptance(request, score)["passed"] is True
    assert runner.raw_acceptance(request, {"errors": []})["passed"] is False
    assert runner.raw_acceptance(request, None)["passed"] is None


def test_raw_acceptance_does_not_treat_tolerated_upper_violation_as_pass():
    request, score = single_condition(None, .01, .010000000000001)
    assert runner.raw_acceptance(request, score)["passed"] is False


def test_historical_frozen_suite_is_explicitly_unsupported_and_unchanged():
    original = SUITE.read_bytes()
    with pytest.raises(ValueError, match="Unsupported legacy request format"):
        runner.compile_preflight(SUITE, seed=0, workers=1)
    assert SUITE.read_bytes() == original


def test_real_tiny_execute_job_retains_counters_audit_and_closes_executor(tmp_path):
    compiled = runner.compile_preflight(small_suite(tmp_path), workers=1)
    result = runner.execute_run(compiled["runs"][0], tmp_path / "production-run",
        compiled, Event(), Event())
    assert result["error"] is None
    summary = result["summary"]
    assert summary["cleanup"]["verified"] is True
    assert summary["independent_audit_available"] is True
    assert summary["candidate_audit_matches"] is True
    assert summary["strict_raw_targets_met"] is True
    assert 0 <= summary["first_audited_success_seconds"] <= summary["elapsed_seconds"]
    assert summary["work_counters"]["evaluations"] == 2
    assert summary["work_counters"]["audit_physical_evaluations"] > 0
    assert summary["actual_training_physics"]["training_physical_evaluations"] > 0
    directory = tmp_path / "production-run"
    assert (directory / "best-candidate.json").exists()
    events = [json.loads(line) for line in (directory / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0]["kind"] == "startup"
    assert events[-1]["kind"] == "terminal"
    assert [row["sequence"] for row in events] == list(range(1, len(events) + 1))
    assert result["response"]["execution_project_root"] == str(directory)


@pytest.fixture(scope="module")
def real_grid_evidence(tmp_path_factory):
    directory = tmp_path_factory.mktemp("runner-real-grid")
    objective = current_objective(540., 560., 3, angles=(0., 30.))
    compiled = runner.compile_preflight(small_suite(directory, objectives=[objective]), workers=1)
    result = runner.execute_run(compiled["runs"][0], directory / "actual-production",
        compiled, Event(), Event())
    assert result["error"] is None
    assert result["summary"]["strict_raw_targets_met"] is True
    return runner.jsonable(compiled["runs"][0]["frozen_request"]), runner.jsonable(result["response"])


def test_actual_train_and_audit_require_all_cartesian_conditions(real_grid_evidence):
    request, response = real_grid_evidence
    for field, scope in (("training_score", "training"), ("validation_score", "audit")):
        score = deepcopy(response["result"][field])
        assert runner.raw_acceptance(request, score, scope=scope)["passed"] is True
        # Both angle values and the objective remain present: seen indices alone
        # would incorrectly accept this missing wavelength/angle condition.
        score["errors"].pop(1)
        verdict = runner.raw_acceptance(request, score, scope=scope)
        assert verdict["passed"] is False
        assert verdict["missing_condition_count"] == 1


@pytest.mark.parametrize("corruption", ["duplicate", "wrong_metric", "wrong_polarization", "wrong_angle", "outside_band"])
def test_actual_audit_rejects_invalid_or_duplicate_conditions(real_grid_evidence, corruption):
    request, response = real_grid_evidence
    score = deepcopy(response["result"]["validation_score"])
    if corruption == "duplicate":
        score["errors"].append(deepcopy(score["errors"][0]))
    else:
        key, value = {"wrong_metric": ("metric", "T"), "wrong_polarization": ("polarization", "p"),
            "wrong_angle": ("angle_deg", 31.), "outside_band": ("wavelength_nm", 600.)}[corruption]
        score["errors"][0][key] = value
    assert runner.raw_acceptance(request, score, scope="audit")["passed"] is False


def test_actual_audit_requires_offset_points_even_with_every_dense_point(real_grid_evidence):
    import numpy as np
    request, response = real_grid_evidence
    score = deepcopy(response["result"]["validation_score"])
    objective = request["objectives"][0]
    count = (objective["wavelength_samples"] - 1) * request["validation_samples_multiplier"] + 1
    dense = set(float(wave) for wave in np.linspace(objective["wavelength_min_nm"], objective["wavelength_max_nm"], count))
    before = len(score["errors"])
    score["errors"] = [row for row in score["errors"] if row["wavelength_nm"] in dense]
    assert len(score["errors"]) < before
    # Make the claimed row count internally consistent; coverage must still fail.
    score["audit"]["unique_condition_points"] = len(score["errors"])
    verdict = runner.raw_acceptance(request, score, scope="audit")
    assert verdict["missing_condition_count"] > 0
    assert verdict["passed"] is False


@pytest.mark.parametrize("field,value", [("completed_extrema", 999), ("dense_points", 0),
    ("offset_points", 0), ("continuous_certificate", True), ("unique_condition_points", 1)])
def test_actual_audit_rejects_inconsistent_completion_metadata(real_grid_evidence, field, value):
    request, response = real_grid_evidence
    score = deepcopy(response["result"]["validation_score"])
    score["audit"][field] = value
    assert runner.raw_acceptance(request, score, scope="audit")["passed"] is False


def test_candidate_binding_allows_only_top_level_presentation_annotations(real_grid_evidence):
    _, response = real_grid_evidence
    body = deepcopy(response["result"])
    body["candidate_design"]["note"] = "Explicit unknown-k annotation added by production result presentation"
    assert runner.candidate_evidence_matches(body["candidate_design"], body["training_score"], body["validation_score"])
    body["validation_score"]["design"]["front_layers"].append(dict(id="another-layer", material_id="material",
        optical_set_id="set", thickness=dict(value=100., unit="nm")))
    assert not runner.candidate_evidence_matches(body["candidate_design"], body["training_score"], body["validation_score"])


@pytest.mark.parametrize("corruption", ["different_candidate", "missing_training_point", "missing_audit_point",
    "invalid_result_kind"])
def test_terminal_success_and_first_success_reject_corrupted_real_evidence(tmp_path, monkeypatch, corruption):
    from coat_sim.core import film_synthesis_jobs
    actual_execute = film_synthesis_jobs.execute_job
    def execute_then_corrupt(*args, **kwargs):
        response = runner.jsonable(actual_execute(*args, **kwargs))
        assert film_synthesis_jobs.is_synthesis_result(response)
        body = response["result"]
        if corruption == "different_candidate":
            body["candidate_design"]["substrate"]["thickness"]["value"] += 1
        elif corruption == "missing_training_point":
            body["training_score"]["errors"].pop()
        elif corruption == "missing_audit_point":
            body["validation_score"]["errors"].pop()
        else:
            response["kind"] = "invalid_synthesis_result_kind"
        return response
    monkeypatch.setattr(film_synthesis_jobs, "execute_job", execute_then_corrupt)
    compiled = runner.compile_preflight(small_suite(tmp_path), workers=1)
    directory = tmp_path / "corrupt-evidence"
    result = runner.execute_run(compiled["runs"][0], directory, compiled, Event(), Event())
    assert result["error"]["type"] == "ValueError"
    assert result["error"]["message"] == "Production execute_job returned an invalid synthesis result"
    summary = result["summary"]
    assert summary["state"] == "runner_failed"
    assert summary["targets_met"] is False
    assert summary["strict_raw_targets_met"] is False
    assert summary["first_audited_success_seconds"] is None
    assert summary["candidate_audit_matches"] is False
    assert summary["independent_audit_available"] is False
    assert summary["cleanup"]["verified"] is False
    assert summary["training_raw_bounds"]["available"] is False
    assert summary["audit_raw_bounds"]["available"] is False
    # Rejected production data is preserved for diagnosis, never trusted as
    # terminal success or a cleanup certificate, even with intact optics.
    assert result["response"]["result"]["state"] == "completed"
    assert result["response"]["result"]["targets_met"] is True
    if corruption == "invalid_result_kind":
        body = result["response"]["result"]
        assert runner.candidate_evidence_matches(body["candidate_design"], body["training_score"], body["validation_score"])
        assert runner.raw_acceptance(compiled["runs"][0]["frozen_request"], body["training_score"])["passed"] is True
        assert runner.raw_acceptance(compiled["runs"][0]["frozen_request"], body["validation_score"], scope="audit")["passed"] is True
    saved = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    assert saved == result
    events = [json.loads(line) for line in (directory / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-2]["kind"] == "error"
    assert events[-1]["kind"] == "terminal"
    assert events[-1]["summary"] == summary
    assert events[-1]["error"] == result["error"]


@pytest.mark.parametrize("corrupt_body", [False, True])
def test_rejected_non_object_response_or_body_still_writes_failed_terminal(tmp_path, monkeypatch, corrupt_body):
    from coat_sim.core import film_synthesis_jobs
    actual_execute = film_synthesis_jobs.execute_job
    def execute_then_corrupt(*args, **kwargs):
        response = runner.jsonable(actual_execute(*args, **kwargs))
        if corrupt_body:
            response["result"] = []
            return response
        return []
    monkeypatch.setattr(film_synthesis_jobs, "execute_job", execute_then_corrupt)
    compiled = runner.compile_preflight(small_suite(tmp_path), workers=1)
    directory = tmp_path / "invalid-shape"
    result = runner.execute_run(compiled["runs"][0], directory, compiled, Event(), Event())
    assert result["error"]["message"] == "Production execute_job returned an invalid synthesis result"
    if corrupt_body:
        assert result["response"]["result"] == []
    else:
        assert result["response"] == []
    assert result["summary"]["state"] == "runner_failed"
    assert result["summary"]["targets_met"] is False
    assert result["summary"]["strict_raw_targets_met"] is False
    assert result["summary"]["first_audited_success_seconds"] is None
    assert json.loads((directory / "result.json").read_text(encoding="utf-8")) == result
    events = [json.loads(line) for line in (directory / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["kind"] == "terminal"
    assert events[-1]["summary"] == result["summary"]


def test_rejected_result_does_not_count_as_suite_raw_success(tmp_path, monkeypatch):
    from coat_sim.core import film_synthesis_jobs
    actual_execute = film_synthesis_jobs.execute_job
    def execute_then_corrupt(*args, **kwargs):
        response = runner.jsonable(actual_execute(*args, **kwargs))
        response["kind"] = "invalid_synthesis_result_kind"
        return response
    monkeypatch.setattr(film_synthesis_jobs, "execute_job", execute_then_corrupt)
    output = tmp_path / "invalid-suite-result"
    assert runner.main(["--suite", str(small_suite(tmp_path)), "--workers", "1", "--output", str(output)]) == 1
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["state"] == "partial_or_invalid"
    assert result["complete"] is True
    assert result["valid_execution"] is False
    assert result["strict_raw_success_count"] == 0
    assert result["runs"][0]["summary"]["state"] == "runner_failed"
    assert result["runs"][0]["summary"]["targets_met"] is False
    assert result["runs"][0]["summary"]["first_audited_success_seconds"] is None


def test_mismatched_checkpoint_cannot_supply_first_success_time(tmp_path, monkeypatch):
    from coat_sim.core import film_synthesis_jobs
    actual_execute = film_synthesis_jobs.execute_job
    def execute_with_corrupted_checkpoint(value, cancel, report, path, write_json, root):
        def corrupt_checkpoint(checkpoint_path, record):
            copied = deepcopy(record)
            copied["design"]["substrate"]["thickness"]["value"] += 1
            write_json(checkpoint_path, copied)
        return actual_execute(value, cancel, report, path, corrupt_checkpoint, root)
    monkeypatch.setattr(film_synthesis_jobs, "execute_job", execute_with_corrupted_checkpoint)
    compiled = runner.compile_preflight(small_suite(tmp_path), workers=1)
    directory = tmp_path / "mismatched-checkpoint"
    result = runner.execute_run(compiled["runs"][0], directory, compiled, Event(), Event())
    assert result["error"] is None
    summary = result["summary"]
    assert summary["strict_raw_targets_met"] is True
    assert summary["first_audited_success_seconds"] == summary["elapsed_seconds"]
    events = [json.loads(line) for line in (directory / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
    checkpoints = [row for row in events if row["kind"] == "checkpoint"]
    assert checkpoints
    assert all(row["candidate_audit_matches"] is False for row in checkpoints)
