"""Independent adversarial checks for the synthesis execution boundary.

The fixtures use current frozen materials and actual scoring. No production
source, live model, scheduler daemon, or background worker is patched here.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
from threading import Event
from typing import Any, Mapping

import pytest

from coat_sim.core.api import CoatService
from coat_sim.core.film_auto_synthesis import (
    FilmSynthesisRequest, _proxy, freeze_film_synthesis_request, synthesize_film_design,
)
from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
from coat_sim.core.film_physical_structures import (
    FilmOpticalLayerSpec, FilmStructureMaterialBinding, FilmStructureSpec,
    generate_physical_structure,
)
from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate
from coat_sim.core.film_synthesis_io import (
    film_synthesis_request_to_payload, film_synthesis_result_to_payload,
)
from coat_sim.core.film_synthesis_jobs import (
    AUDITED_CHECKPOINT_KIND, build_job_payload, canonical_parameters, execute_job, is_synthesis_result,
    submit_synthesis, validate_checkpoint,
)
from coat_sim.scheduler.io import write_json, write_text
from coat_sim.scheduler.jobs import JobSpec, canonical
from coat_sim.scheduler.result_capability import is_best_candidate_result, load_best_candidate_checkpoint
from coat_sim.scheduler.store import SchedulerStore
from tests.test_assistant_discussion_mode import _actual_run
from tests.test_film_auto_synthesis import setup_problem


def _request(*, revision: int = 29, ceiling: float = 1.0,
             maximum_nm: float = 50.0) -> FilmSynthesisRequest:
    service, request = setup_problem(
        upper=ceiling, low=550.0, high=550.0, samples=1, materials=("low",))
    return freeze_film_synthesis_request(service, replace(
        request, design_revision=revision, max_layer_thickness_nm=maximum_nm,
        max_cycles=1, max_evaluations=12))


def _job(request: FilmSynthesisRequest, request_id: str = "independent-execution") -> dict[str, Any]:
    payload = film_synthesis_request_to_payload(request_id, request)
    return build_job_payload(canonical_parameters(None, {"payload": payload}))


def _score_json(score: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(score))
    result["errors"] = [asdict(error) for error in score["errors"]]
    return result


def _checkpoint(request: FilmSynthesisRequest, *, layer: bool = False,
                supplied_design: Mapping[str, Any] | None = None) -> dict[str, Any]:
    design = deepcopy(dict(request.environment if supplied_design is None else supplied_design))
    if layer:
        design["front_layers"] = [{
            "id": "saved-ar", "material_id": "low", "optical_set_id": "nk",
            "thickness": {"value": 112.26827987756234, "unit": "nm"},
            "note": "Synthetic reference layer retained from an audited candidate",
        }]
    training = evaluate_frozen_structure_candidate(_proxy(request), design)["score"]
    audited = audit_frozen_synthesis_candidate(request, design)
    return {
        "schema_version": 1, "kind": AUDITED_CHECKPOINT_KIND,
        "checkpoint_state": "audited", "job": _job(request), "design": design,
        "training_score": _score_json(training),
        "validation_score": _score_json(audited["score"]),
        "audit": deepcopy(audited["audit"]),
        "progress": {"cycle": 1, "evaluations": 3, "layer_count": len(design["front_layers"])},
        "physical_structure_metadata": [],
    }


def test_discussion_actual_dispatch_cannot_submit_synthesis_or_register_estimate():
    payload = film_synthesis_request_to_payload("discussion-synthesis", _request())
    calls = [
        ("submit_film_synthesis", {"payload": payload}),
        ("estimate_compute_task", {"kind": "film_synthesis", "parameters": {"payload": payload}}),
        ("submit_film_synthesis", {"payload": payload, "mode": "research", "approved": True}),
    ]
    rpc, returned, _tools = asyncio.run(_actual_run(calls, mode="chat"))
    assert rpc == []
    assert len(returned) == len(calls)
    for reply in returned:
        assert reply.content["error"]["code"] == "discussion_read_only"
        assert reply.content["error"]["effect"] == "none"


def test_prepared_synthesis_identity_is_confined_to_each_real_business_root(tmp_path: Path):
    root_a, root_b = tmp_path / "business-a", tmp_path / "business-b"
    payload = film_synthesis_request_to_payload("same-input", _request())
    first = submit_synthesis(CoatService(root_a), payload, "same-client")
    replay = submit_synthesis(CoatService(root_a), payload, "same-client")
    other = submit_synthesis(CoatService(root_b), payload, "same-client")
    assert first["state"] == replay["state"] == other["state"] == "prepared"
    assert first["estimate_id"] == replay["estimate_id"]
    assert first["estimate_id"] != other["estimate_id"]
    assert first["execution_project_root"] == replay["execution_project_root"] == str(root_a.resolve())
    assert other["execution_project_root"] == str(root_b.resolve())
    assert SchedulerStore(root_a).queue.snapshot()["jobs"] == []
    assert SchedulerStore(root_b).queue.snapshot()["jobs"] == []


def test_cancelled_checkpoint_keeps_nonzero_revision_and_actual_layer_table():
    request = _request(revision=73, ceiling=0.001, maximum_nm=200.0)
    checked = validate_checkpoint(_checkpoint(request, layer=True), terminal_state="cancelled")
    result = checked["result"]
    assert result["state"] == "cancelled" and not result["targets_met"]
    assert result["design_revision"] == result["frozen_request"]["design_revision"] == 73
    assert result["validation_complete"]
    rows = checked["review"]["layers"]
    assert len(rows) == 1
    assert rows[0]["id"] == "saved-ar" and rows[0]["material_id"] == "low"
    assert rows[0]["thickness_nm"] == pytest.approx(112.26827987756234)
    assert result["validation_score"]["max_error"] < 1e-20


@pytest.mark.parametrize("fault", ["environment", "physical_units", "missing_validation", "nonfinite_validation"])
def test_checkpoint_replay_rejects_changed_environment_constraints_and_invalid_score(fault: str):
    checkpoint = _checkpoint(_request())
    if fault == "environment":
        checkpoint["design"]["incident_medium"]["n"] = 2.0
    elif fault == "physical_units":
        # Actual 100 nm exceeds the frozen 50 nm bound even though raw value .1 does not.
        checkpoint["design"]["front_layers"] = [{
            "id": "unit-bypass", "material_id": "low", "optical_set_id": "nk",
            "thickness": {"value": 0.1, "unit": "um"}, "note": "Modified checkpoint",
        }]
    elif fault == "missing_validation":
        checkpoint["validation_score"] = {}
    else:
        checkpoint["validation_score"]["max_error"] = float("nan")
    with pytest.raises(ValueError):
        validate_checkpoint(checkpoint)


def test_executor_checkpoint_survives_actual_atomic_json_write_and_read(tmp_path: Path):
    request = _request(revision=83, ceiling=0.001, maximum_nm=200.0)
    job = _job(request)
    checkpoint_path = tmp_path / "best-candidate.json"
    result = execute_job(
        {"payload": job}, Event(), lambda _value, **_options: None,
        checkpoint_path, write_json, str(tmp_path.resolve()))
    assert result["result"]["state"] == "completed"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    restored = validate_checkpoint(checkpoint, terminal_state="cancelled")
    assert restored["result"]["design_revision"] == 83
    assert restored["result"]["validation_complete"]
    assert restored["review"]["layers"] == result["review"]["layers"]


@pytest.mark.parametrize("fault", ["foreign_checkpoint", "replaced_spec"])
def test_scheduler_recovery_binds_checkpoint_to_job_spec_and_business_root(tmp_path: Path, fault: str):
    root_a, root_b = tmp_path / "business-a", tmp_path / "business-b"
    request = _request(revision=97)
    checkpoint = _checkpoint(request)
    job_id = "d" * 32
    spec = JobSpec("film_synthesis", {"payload": checkpoint["job"]}, {}).to_dict()
    store = SchedulerStore(root_a)
    store.submit(spec, job_id=job_id)
    directory = root_a / "artifacts/runtime/scheduler/jobs" / job_id
    write_text(directory / "spec.json", canonical(spec))
    write_json(directory / "best-candidate.json", checkpoint)
    restored, reason = load_best_candidate_checkpoint(root_a, job_id)
    assert restored is not None, reason
    assert restored["execution_project_root"] == str(root_a.resolve())
    assert restored["result"]["design_revision"] == 97
    foreign, _reason = load_best_candidate_checkpoint(root_b, job_id)
    assert foreign is None and not root_b.exists()
    changed = _job(request, request_id="different-request")
    if fault == "foreign_checkpoint":
        checkpoint["job"] = changed
        write_json(directory / "best-candidate.json", checkpoint)
    else:
        write_text(directory / "spec.json", canonical(JobSpec("film_synthesis", {"payload": changed}, {}).to_dict()))
    rejected, reason = load_best_candidate_checkpoint(root_a, job_id)
    assert rejected is None and "bound" in reason


def test_generated_physical_seed_checkpoint_retains_legal_identity_metadata():
    request = _request(revision=101, ceiling=0.001, maximum_nm=200.0)
    assert request.frozen_material_snapshot is not None
    spec = FilmStructureSpec(
        "composite", (FilmStructureMaterialBinding("L", "low", "nk"),),
        (FilmOpticalLayerSpec("L", 550.0),), name="Generated AR seed identity")
    generated = generate_physical_structure(
        request.environment, spec, request.frozen_material_snapshot).candidate_design
    assert generated["id"] != request.environment["id"]
    assert generated["name"] != request.environment["name"]
    assert generated["note"] != request.environment["note"]
    frozen = freeze_film_synthesis_request(None, replace(request, seed_designs=(generated,)))
    checkpoint = _checkpoint(frozen, supplied_design=generated)
    restored = validate_checkpoint(checkpoint, terminal_state="cancelled")
    assert restored["result"]["design_revision"] == 101
    assert restored["result"]["candidate_design"] == generated
    assert restored["result"]["validation_complete"]
    assert not restored["result"]["targets_met"]


def _generated_parameters() -> dict[str, Any]:
    from coat_sim.core.film_physical_structures import structure_spec_to_mapping

    request = _request(revision=107, ceiling=0.001, maximum_nm=200.0)
    spec = FilmStructureSpec(
        "composite", (FilmStructureMaterialBinding("L", "low", "nk"),),
        (FilmOpticalLayerSpec("L", 550.0),), name="Bound physical provenance")
    payload = film_synthesis_request_to_payload(
        "provenance-adversary", request, (structure_spec_to_mapping(spec),))
    return canonical_parameters(None, {"payload": payload})


def test_physical_provenance_replays_and_rejects_modified_real_library_claims():
    parameters = _generated_parameters()
    canonical = canonical_parameters(None, parameters)
    assert canonical == parameters
    assert build_job_payload(canonical)["physical_structure_metadata"] == parameters["physical_structure_metadata"]
    changed = deepcopy(parameters)
    changed["physical_structure_metadata"][0]["metadata"]["references"] = ["https://doi.org/10.0000/forged"]
    with pytest.raises(ValueError, match="metadata"):
        canonical_parameters(None, changed)
    with pytest.raises(ValueError, match="metadata"):
        build_job_payload(changed)


def test_typed_worker_envelope_rejects_forged_library_provenance(tmp_path: Path):
    job = build_job_payload(_generated_parameters())
    job["physical_structure_metadata"][0]["metadata"]["references"] = ["https://doi.org/10.0000/forged"]
    with pytest.raises(ValueError):
        execute_job({"payload": job}, Event(), lambda _value, **_options: None,
                    tmp_path / "best-candidate.json", write_json, str(tmp_path.resolve()))


def test_remote_admission_rejects_forged_library_provenance():
    from coat_sim.scheduler.remote_contract import _validate_film_synthesis_payload

    job = build_job_payload(_generated_parameters())
    job["physical_structure_metadata"][0]["metadata"]["approximations"] = ["Certified exact manufacture"]
    with pytest.raises(ValueError):
        _validate_film_synthesis_payload(job)


def test_scheduler_cancel_wins_over_completed_result_without_success_claim(tmp_path: Path):
    from coat_sim.scheduler.daemon import Scheduler

    job = _job(_request(revision=109, ceiling=0.001, maximum_nm=200.0))
    completed = execute_job(
        {"payload": job}, Event(), lambda _value, **_options: None,
        tmp_path / "best-candidate.json", write_json, str(tmp_path.resolve()))
    assert completed["result"]["state"] == "completed"
    assert completed["result"]["targets_met"]
    job_id = "cancel-wins-completed-synthesis"
    SchedulerStore(tmp_path).submit(JobSpec("film_synthesis", {"payload": job}, {}).to_dict(), job_id=job_id)
    scheduler = Scheduler(tmp_path)
    try:
        outcome = scheduler._cancelled_outcome(job_id, {"state": "succeeded", "result": completed})
    finally:
        scheduler.guard.close()
        scheduler.lock.close()
    result = outcome["result"]
    assert outcome["state"] == result["result"]["state"] == "cancelled"
    assert not result["result"]["targets_met"]
    assert not result["review"]["targets_met"]
    assert result["review"]["termination"] == {
        "state": "cancelled", "reason": result["result"]["termination_reason"]}
    assert result["result"]["validation_complete"]
    assert result["review"]["layers"] == completed["review"]["layers"]
    assert result["best_candidate_audit"] == completed["best_candidate_audit"]
    assert result["best_candidate_audit"]["available"] is True
    assert result["best_candidate_audit"]["targets_met"] is True
    assert result["best_candidate_audit"]["continuous_certificate"] is False
    assert result["result"]["training_score"] == completed["result"]["training_score"]
    assert result["result"]["validation_score"] == completed["result"]["validation_score"]
    assert result["review"]["per_objective"] == completed["review"]["per_objective"]
    assert all(row["targets_met"] for row in result["review"]["per_objective"])


def test_core_cancel_before_any_audit_reports_unknown_candidate_quality(tmp_path: Path):
    request = _request(revision=113, ceiling=0.001, maximum_nm=200.0)
    original = deepcopy(request.environment)
    cancelled = Event()
    cancelled.set()
    result = synthesize_film_design(None, request, cancel_event=cancelled)
    response = film_synthesis_result_to_payload("cancel-before-audit", result)
    assert result.state == "cancelled" and not result.targets_met
    assert result.candidate_design is None and not result.validation_complete
    assert not response["review"]["targets_met"]
    audit = response["best_candidate_audit"]
    assert audit["available"] is False and audit["targets_met"] is None
    assert audit["training_max_normalized_error"] is None
    assert audit["audit_max_normalized_error"] is None
    assert audit["acceptable_normalized_error"] == request.acceptable_normalized_error
    assert audit["continuous_certificate"] is False
    assert request.environment == original
    checkpoint_path = tmp_path / "best-candidate.json"
    executed = execute_job(
        {"payload": _job(request)}, cancelled, lambda _value, **_options: None,
        checkpoint_path, write_json, str(tmp_path.resolve()))
    assert executed["result"]["state"] == "cancelled"
    assert not executed["result"]["targets_met"]
    assert executed["result"]["candidate_design"] is None
    assert not executed["result"]["validation_complete"]
    assert executed["best_candidate_audit"] == audit
    assert is_synthesis_result(executed)
    assert not is_best_candidate_result(executed)
    assert not checkpoint_path.exists()


def test_core_cancel_after_qualified_audit_preserves_physical_truth_without_application():
    request = _request(revision=127, ceiling=0.001, maximum_nm=200.0)
    original = deepcopy(request.environment)
    cancelled = Event()
    retained: list[tuple[Any, Any, Any, Any]] = []

    def snapshot(design, training, validation, audit):
        epsilon = request.acceptable_normalized_error
        if training["max_error"] <= epsilon and validation["max_error"] <= epsilon:
            retained.append(deepcopy((design, training, validation, audit)))
            cancelled.set()

    result = synthesize_film_design(
        None, request, cancel_event=cancelled, snapshot_callback=snapshot)
    assert cancelled.is_set() and len(retained) == 1
    design, training, validation, _audit = retained[0]
    assert result.state == "cancelled" and not result.targets_met
    assert result.validation_complete and result.design_revision == 127
    assert result.candidate_design == design and len(design["front_layers"]) == 1
    assert design["front_layers"][0]["thickness"]["value"] > 0
    assert result.training_score == training and result.validation_score == validation
    response = film_synthesis_result_to_payload("cancel-qualified-audit", result)
    assert response["review"]["termination"]["state"] == "cancelled"
    assert not response["review"]["targets_met"]
    candidate = response["best_candidate_audit"]
    assert candidate["available"] is True and candidate["targets_met"] is True
    assert candidate["training_max_normalized_error"] == training["max_error"]
    assert candidate["audit_max_normalized_error"] == validation["max_error"]
    assert candidate["acceptable_normalized_error"] == request.acceptable_normalized_error
    assert candidate["continuous_certificate"] is False
    assert response["review"]["per_objective"] == validation["audit"]["objective_status"]
    assert all(row["targets_met"] for row in response["review"]["per_objective"])
    assert response["result"]["candidate_design"] == design
    assert request.environment == original and request.environment["front_layers"] == []
