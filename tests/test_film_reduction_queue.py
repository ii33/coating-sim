"""Real child worker and durable checkpoint wiring, separate from optics effects."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable
from coat_sim.core.film_reduction import FilmReductionRequest, FilmReductionResult, film_reduction_result_from_payload
from coat_sim.scheduler.jobs import canonical, freeze_spec
from coat_sim.scheduler.result_capability import is_film_optimization_result
from coat_sim.scheduler.store import SchedulerStore
from coat_sim.ui.film_optimization_quick_controller import _film_result_from_queue
from tests.film_structure_fixtures import fixture
from tests.scheduler_queue_support import SchedulerHarness

pytestmark = [pytest.mark.contract, pytest.mark.qt]
ROOT = Path(__file__).resolve().parents[1]


def test_real_child_worker_persists_reduction_checkpoint_and_typed_final_result(tmp_path):
    service, _, design = fixture(tmp_path)
    layer_id = design["front_layers"][0]["id"]
    objective = FilmOptimizationObjective(500, 600, 3, angles_deg=(0.0, 20.0),
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0.0, upper_bound=1.0))
    request = FilmReductionRequest(FilmOptimizationRequest(design, 19,
        (FilmThicknessVariable("front", layer_id, 0.0, 200.0),), (objective,),
        max_iterations=2, max_evaluations=8), "layer_count")
    spec = freeze_spec(service, "film_optimization", {"request": request})
    job_id, instance = uuid4().hex, uuid4().hex
    directory = tmp_path / "artifacts/runtime/scheduler/jobs" / job_id
    directory.mkdir(parents=True)
    SchedulerStore(tmp_path).submit(spec, job_id=job_id)
    (directory / "spec.json").write_text(canonical(spec), encoding="utf-8")
    (directory / "launch.json").write_text(json.dumps({"instance": instance}), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    completed = subprocess.run([sys.executable, "-c",
        "from coat_sim.scheduler.worker import run; import sys; "
        "raise SystemExit(run(sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4]))",
        str(tmp_path), job_id, str(os.getpid()), instance], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    outcome = json.loads((directory / "outcome.json").read_text(encoding="utf-8"))
    assert outcome["state"] == "succeeded", outcome
    payload = outcome["result"]
    assert is_film_optimization_result(payload)
    result = film_reduction_result_from_payload(payload)
    assert result.is_final and result.candidate_verified
    assert result.summary["reduction_achieved"]
    assert result.base_result.validation_complete
    assert result.metrics["candidate"]["layer_count"] == 0
    decoded = _film_result_from_queue(payload)
    assert isinstance(decoded, FilmReductionResult)
    assert decoded.frozen_request == result.frozen_request
    checkpoint = json.loads((directory / "best-candidate.json").read_text(encoding="utf-8"))
    partial = film_reduction_result_from_payload(checkpoint["result"])
    assert not partial.is_final and partial.candidate_verified
    assert partial.frozen_request == result.frozen_request
    evidence = ROOT / "artifacts/reduction/worker-evidence.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps({"returncode": completed.returncode,
        "stdout": completed.stdout, "stderr": completed.stderr,
        "outcome": outcome, "checkpoint": checkpoint}, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("cancel_at_completion", [False, True])
def test_real_scheduler_preserves_reduction_envelope_through_terminal_receipt(
        tmp_path, cancel_at_completion):
    """Cover the daemon finalization omitted by the direct-child regression."""
    service, _, design = fixture(tmp_path)
    layer_id = design["front_layers"][0]["id"]
    objective = FilmOptimizationObjective(500, 600, 3, angles_deg=(0.0, 20.0),
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0.0, upper_bound=1.0))
    request = FilmReductionRequest(FilmOptimizationRequest(design, 19,
        (FilmThicknessVariable("front", layer_id, 0.0, 200.0),), (objective,),
        max_iterations=2, max_evaluations=8), "layer_count")
    spec = freeze_spec(service, "film_optimization", {"request": request})
    harness = SchedulerHarness(tmp_path)
    try:
        client = harness.client()
        submitted = client.submit(spec)
        job_id = submitted["job_id"]
        harness.tick()
        assert harness.scheduler.active is not None
        _, process, directory, _ = harness.scheduler.active
        # The real child completes before the daemon consumes its terminal
        # slot, making cancellation at this boundary deterministic.
        deadline = time.monotonic() + 30
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert process.poll() == 0
        worker = json.loads((directory / "outcome.json").read_text(encoding="utf-8"))
        assert worker["state"] == "succeeded", worker
        if cancel_at_completion:
            assert client.cancel(job_id)["cancel_requested"]
        harness.tick()
        row = client.status(job_id)
        assert row["state"] == ("cancelled" if cancel_at_completion else "succeeded"), row
        # The live UI observer receives the terminal row directly. Exercise
        # that reader before the separate historical result RPC as well.
        payload = row["result"]
        decoded = _film_result_from_queue(payload)
        assert "result_artifacts" not in payload
        assert set(payload) == set(worker["result"])
        assert is_film_optimization_result(payload)
        assert isinstance(decoded, FilmReductionResult)
        assert decoded.state == ("cancelled" if cancel_at_completion else "completed")
        assert decoded.candidate_verified and decoded.base_result.validation_complete
        assert decoded.summary["reduction_achieved"]
        assert decoded.best_design == worker["result"]["candidate"]["design"]
        assert decoded.metrics["candidate"]["layer_count"] == 0
        assert client.result(job_id) == payload
        # Durable history is the same strict envelope as the live receipt.
        assert harness.scheduler.store.status(job_id)["result"] == payload
        if not cancel_at_completion:
            assert payload == worker["result"]
    finally:
        harness.close()


@pytest.mark.parametrize("mutation", ["legacy_version", "unknown_field"])
def test_reduction_reader_still_rejects_old_or_extended_results(mutation):
    from coat_sim.core.film_reduction import reduce_film_design, film_reduction_result_to_payload
    from tests.test_film_reduction import Service, request

    result = reduce_film_design(Service(), request())
    payload = film_reduction_result_to_payload(result.frozen_request, result)
    if mutation == "legacy_version":
        payload["schema_version"] = 1
    else:
        payload["result_artifacts"] = []
    assert not is_film_optimization_result(payload)
    with pytest.raises(ValueError, match="film reduction result kind/version/fields are invalid"):
        _film_result_from_queue(payload)
