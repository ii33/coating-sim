"""Worker-level contracts for the opt-in deferred film batch mode."""
from __future__ import annotations

from dataclasses import replace
from threading import Event
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from coat_sim.core.film_optimization_job import (
    deserialize_request,
    execute_film_optimization_job,
    serialize_request,
    validate_checkpoint,
)
from coat_sim.core.film_performance import PerformanceOptions
from coat_sim.scheduler.jobs import canonical, freeze_spec
from coat_sim.scheduler.store import SchedulerStore
from tests.test_film_optimization_forward_cache import _request
from tests.film_performance_fixtures import fixture


ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent


def _batch_request(design, frozen, *, budget=12, batch_size=3, iterations=2):
    request = _request(
        design, frozen, solver="differential_evolution", samples=3, budget=budget,
    )
    return replace(
        request,
        execution_mode="deferred_batch",
        batch_size=batch_size,
        max_iterations=iterations,
        validation_samples_multiplier=1,
    )


def _frozen_job(service, request):
    spec = freeze_spec(service, "film_optimization", {"request": request})
    return spec, spec["parameters"]["payload"]


def test_serial_request_wire_shape_still_omits_new_defaults():
    service, design, frozen = fixture(layers=4)
    del service
    request = _request(design, frozen)

    wire = serialize_request(request)

    assert "execution_mode" not in wire
    assert "batch_size" not in wire
    restored = deserialize_request(wire)
    assert restored == request
    assert restored.execution_mode == "serial"
    assert restored.batch_size == 16


def test_deferred_batch_request_round_trips_mode_and_batch_size():
    _service, design, frozen = fixture(layers=4)
    request = _batch_request(design, frozen, batch_size=65)

    wire = serialize_request(request)

    assert wire["execution_mode"] == "deferred_batch"
    assert wire["batch_size"] == 65
    assert deserialize_request(wire) == request


def test_old_optimization_sample_and_budget_thresholds_round_trip_without_compute():
    _service, design, frozen = fixture(layers=4)
    request = _request(design, frozen)
    objective = replace(
        request.objectives[0], wavelength_samples=10002,
        angles_deg=tuple(index * 0.24 for index in range(362)),
    )
    request = replace(
        request, objectives=(objective,), max_iterations=10001,
        max_evaluations=100001, multistart_count=1001,
        population_size=1001, validation_samples_multiplier=101,
    )

    # Exercise core validation and immutable queue replay only. The synthetic
    # condition count exceeds every removed safety ceiling, but is not run.
    wire = serialize_request(request)
    restored = deserialize_request(wire)
    assert restored == request
    assert restored.objectives[0].wavelength_samples * len(
        restored.objectives[0].angles_deg) > 10001
    assert restored.max_iterations > 10000
    assert restored.max_evaluations > 100000
    assert restored.multistart_count > 1000
    assert restored.population_size > 1000
    assert restored.validation_samples_multiplier > 100


@pytest.mark.parametrize(("changes", "message"), [
    ({"execution_mode": "unknown"}, "execution_mode"),
    ({"execution_mode": "deferred_batch", "solver": "baseline"},
     "deferred_batch requires differential_evolution"),
    ({"execution_mode": "deferred_batch", "solver": "differential_evolution",
      "batch_size": 0}, "batch_size"),
    ({"execution_mode": "deferred_batch", "solver": "differential_evolution",
      "batch_size": True}, "batch_size"),
])
def test_invalid_execution_mode_solver_and_batch_size_are_rejected(changes, message):
    _service, design, frozen = fixture(layers=4)
    request = _request(design, frozen)

    with pytest.raises(ValueError, match=message):
        serialize_request(replace(request, **changes))


def test_execute_batch_result_and_checkpoints_replay_and_seed_is_repeatable():
    service, design, frozen = fixture(layers=4)
    request = _batch_request(design, frozen, budget=12, batch_size=3, iterations=2)
    _spec, payload = _frozen_job(service, request)
    results = []
    traces = []

    for _ in range(2):
        checkpoints = []
        performance = []
        result = execute_film_optimization_job(
            service, payload, cancel_event=Event(),
            checkpoint_callback=checkpoints.append,
            performance_options=PerformanceOptions(backend="cpu"),
            performance_callback=performance.append,
        )
        assert result["state"] == "completed"
        assert result["request"]["execution_mode"] == "deferred_batch"
        assert result["request"]["batch_size"] == 3
        assert result["request"]["solver"] == "differential_evolution"
        assert 1 <= result["candidate"]["evaluations"] <= request.max_evaluations
        assert validate_checkpoint(service, result) == result
        assert checkpoints
        for checkpoint in checkpoints:
            assert validate_checkpoint(service, checkpoint) == checkpoint
        assert performance
        assert all(row["execution_mode"] == "deferred_batch" for row in performance)
        results.append(result)
        traces.append(checkpoints)

    first, second = (result["candidate"] for result in results)
    assert first["iterations"] == second["iterations"]
    assert first["evaluations"] == second["evaluations"]
    assert first["thickness_nm"] == pytest.approx(second["thickness_nm"], abs=1e-12)
    for field in ("value", "weighted_max_normalized_error", "weighted_rms_error"):
        assert first["quality"][field] == pytest.approx(second["quality"][field], abs=1e-13)
    assert len(traces[0]) == len(traces[1])
    for left, right in zip(traces[0], traces[1]):
        assert left["candidate"]["evaluations"] == right["candidate"]["evaluations"]
        assert left["candidate"]["thickness_nm"] == pytest.approx(
            right["candidate"]["thickness_nm"], abs=1e-12)


def test_exact_budget_tail_submits_bounded_final_chunk_and_charges_every_slot():
    service, design, frozen = fixture(layers=4)
    # One mandatory baseline leaves six slots. The five-row initialization
    # consumes four (the baseline is reused), leaving two for the next chunk.
    request = _batch_request(design, frozen, budget=7, batch_size=3, iterations=2)
    _spec, payload = _frozen_job(service, request)
    performance = []

    result = execute_film_optimization_job(
        service, payload, cancel_event=Event(),
        performance_options=PerformanceOptions(backend="cpu"),
        performance_callback=performance.append,
    )

    candidate_batches = [row["batch_candidates"] for row in performance]
    assert result["candidate"]["evaluations"] == 7
    assert candidate_batches
    assert all(1 <= count <= request.batch_size for count in candidate_batches)
    assert sum(candidate_batches) == 6
    assert candidate_batches[-1] < request.batch_size


def test_precancel_still_evaluates_baseline_and_returns_it_as_best():
    service, design, frozen = fixture(layers=4)
    request = _batch_request(design, frozen, budget=12)
    _spec, payload = _frozen_job(service, request)
    cancel = Event()
    cancel.set()
    performance = []
    checkpoints = []

    result = execute_film_optimization_job(
        service, payload, cancel_event=cancel,
        checkpoint_callback=checkpoints.append,
        performance_options=PerformanceOptions(backend="cpu"),
        performance_callback=performance.append,
    )

    assert result["state"] == "cancelled"
    assert result["solver_validation"]["termination_reason"] == "cancelled"
    assert result["candidate"]["evaluations"] == 1
    assert result["candidate"]["thickness_nm"]["layer-0"] == pytest.approx(65.0)
    assert checkpoints
    assert checkpoints[0]["candidate"]["thickness_nm"]["layer-0"] == pytest.approx(65.0)
    assert performance == []
    assert validate_checkpoint(service, result) == result


def test_cancel_from_performance_callback_discards_and_charges_whole_chunk():
    service, design, frozen = fixture(layers=4)
    request = _batch_request(design, frozen, budget=12, batch_size=3, iterations=2)
    _spec, payload = _frozen_job(service, request)
    cancel = Event()
    performance = []

    def cancel_after_chunk_metadata(row):
        performance.append(row)
        if row.get("batch_candidates", 0) > 1:
            cancel.set()

    result = execute_film_optimization_job(
        service, payload, cancel_event=cancel,
        performance_options=PerformanceOptions(backend="cpu"),
        performance_callback=cancel_after_chunk_metadata,
    )

    assert result["state"] == "cancelled"
    assert result["solver_validation"]["termination_reason"] == "cancelled"
    assert performance
    chunk_size = next(row["batch_candidates"] for row in performance
                      if row.get("batch_candidates", 0) > 1)
    assert result["candidate"]["evaluations"] == 1 + chunk_size
    assert result["candidate"]["thickness_nm"]["layer-0"] == pytest.approx(65.0)


def test_gpu_unavailable_mock_falls_back_and_reports_cpu(monkeypatch):
    service, design, frozen = fixture(layers=4)
    request = _batch_request(design, frozen, budget=8, batch_size=3, iterations=1)
    _spec, payload = _frozen_job(service, request)
    monkeypatch.setitem(sys.modules, "coat_sim.core.film_optics_gpu", None)
    performance = []

    execute_film_optimization_job(
        service, payload, cancel_event=Event(),
        performance_options=PerformanceOptions(backend="gpu"),
        performance_callback=performance.append,
    )

    assert performance
    assert all(row["actual_backend"] == "cpu" for row in performance)
    assert all(row["dtype"] == "float64/complex128" for row in performance)
    assert all(row.get("fallback_reason") for row in performance)


def test_failed_batch_keeps_baseline_candidate_and_marks_job_failed(monkeypatch):
    from coat_sim.core.film_optimization_batch_forward import OptimizationBatchForward

    service, design, frozen = fixture(layers=4)
    request = _batch_request(design, frozen, budget=12, batch_size=3, iterations=2)
    _spec, payload = _frozen_job(service, request)
    checkpoints = []

    def fail_batch(self, points, *, cancel=None):
        del self, points, cancel
        raise RuntimeError("controlled optical batch failure")

    monkeypatch.setattr(OptimizationBatchForward, "responses", fail_batch)
    result = execute_film_optimization_job(
        service, payload, cancel_event=Event(),
        checkpoint_callback=checkpoints.append,
        performance_options=PerformanceOptions(backend="cpu"),
    )

    assert result["state"] == "failed"
    assert result["solver_validation"]["termination_reason"] == "batch_evaluation_failed"
    assert result["candidate"]["thickness_nm"]["layer-0"] == pytest.approx(65.0)
    assert result["candidate"]["evaluations"] == 1 + request.batch_size
    assert result["solver_validation"]["failed_evaluations"] == request.batch_size
    assert checkpoints
    assert checkpoints[0]["candidate"]["thickness_nm"]["layer-0"] == pytest.approx(65.0)


@pytest.mark.parametrize(("execution_mode", "cache_enabled", "cpu_workers"), [
    ("serial", False, 1),
    ("serial", True, 1),
    ("deferred_batch", False, 1),
    ("deferred_batch", False, 4),
])
def test_real_scheduler_worker_persists_and_replays_optimization_checkpoint(
        tmp_path, execution_mode, cache_enabled, cpu_workers):
    service, design, frozen = fixture(layers=4)
    request = _batch_request(design, frozen, budget=8, batch_size=3, iterations=2)
    if execution_mode == "serial":
        request = replace(request, execution_mode="serial")
    spec = freeze_spec(service, "film_optimization", {"request": request})
    worker_code = """\
import sys
from tests.film_performance_fixtures import fixture
from coat_sim.core import api

class InMemoryCoatService:
    def __init__(self, root):
        self.film_design_service = fixture(layers=4)[0]

api.CoatService = InMemoryCoatService
from coat_sim.scheduler.worker import run
raise SystemExit(run(sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]))
"""
    root = tmp_path / "scheduler-root"
    root.mkdir()
    job_id = uuid4().hex
    owner_instance = uuid4().hex
    directory = root / "artifacts/runtime/scheduler/jobs" / job_id
    directory.mkdir(parents=True)
    SchedulerStore(root).submit(spec, job_id=job_id)
    (directory / "spec.json").write_text(canonical(spec), encoding="utf-8")
    (directory / "launch.json").write_text(
        json.dumps({"instance": owner_instance}), encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(
        None, (str(ROOT), str(TESTS), env.get("PYTHONPATH", ""))))
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_OPENGL"] = "software"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["COAT_SIM_FILM_BACKEND"] = "cpu"
    env["COAT_SIM_FILM_CPU_WORKERS"] = str(cpu_workers)
    env["COAT_SIM_FILM_FORWARD_CACHE"] = "1" if cache_enabled else "0"
    env["CUDA_VISIBLE_DEVICES"] = ""
    completed = subprocess.run(
        [sys.executable, "-c", worker_code, str(root), job_id,
         str(os.getpid()), owner_instance],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr

    outcome = json.loads((directory / "outcome.json").read_text(encoding="utf-8"))
    assert outcome["state"] == "succeeded", outcome
    result = outcome["result"]
    assert result["state"] == "completed"
    assert result["request"]["execution_mode"] == execution_mode
    assert result["request"]["batch_size"] == 3
    assert validate_checkpoint(service, result) == result

    saved = json.loads((directory / "best-candidate.json").read_text(encoding="utf-8"))
    assert validate_checkpoint(service, saved["result"]) == saved["result"]
    assert saved["result"]["request"]["execution_mode"] == execution_mode

    sidecar_path = directory / "film-performance.json"
    if cache_enabled or execution_mode == "deferred_batch":
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        assert sidecar["actual_backend"] == "cpu"
        if execution_mode == "deferred_batch":
            assert sidecar["execution_mode"] == "deferred_batch"
            assert sidecar["result_transport"] == "in_process_numpy"
            assert sidecar["batch_candidates"] <= request.batch_size
            assert sidecar["batch_forward_calls"] >= 1
            assert 1 <= sidecar["cpu_workers"] <= cpu_workers
        else:
            assert sidecar["cpu_workers"] == 1
    else:
        # The original serial worker has no performance sidecar unless the
        # opt-in forward-cache instrumentation is enabled.
        assert not sidecar_path.exists()

@pytest.mark.parametrize('double,thickness', [(False, 1000.), (True, 1000.), (True, 1000000.)])
def test_batch_multicondition_optical_domains_replay(double, thickness):
    from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmThicknessVariable
    service, design, frozen = fixture(layers=4, double=double, substrate_nm=thickness)
    request = _batch_request(design, frozen, budget=9, batch_size=3)
    variables = (FilmThicknessVariable('front', 'layer-0', 45., 120.),)
    if double:
        variables += (FilmThicknessVariable('back', 'back-0', 40., 110.),)
    request = replace(request, variables=variables, objectives=(
        FilmOptimizationObjective(500., 650., 11, angles_deg=(0., 37.),
                                  polarization='s', metric='R', target_value=0.2),
        FilmOptimizationObjective(530., 670., 9, angles_deg=(15.,),
                                  polarization='p', metric='T', mode='range',
                                  lower_bound=0.4, upper_bound=0.9),
    ))
    _, payload = _frozen_job(service, request)
    checkpoints = []
    result = execute_film_optimization_job(service, payload, cancel_event=Event(),
        checkpoint_callback=checkpoints.append, performance_options=PerformanceOptions(backend='cpu'))
    assert result['state'] == 'completed'
    assert result['candidate']['evaluations'] == 9
    assert len(result['candidate']['per_condition_errors']) == 31
    assert validate_checkpoint(service, result) == result
    for checkpoint in checkpoints:
        assert validate_checkpoint(service, checkpoint) == checkpoint
