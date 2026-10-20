"""Independent ordered-physics and owned-process checks for synthesis parallelism.

The internal startup heuristic is set to zero only in these tiny real-physics
fixtures. Numeric targets, ranks, physical samplers and audits are not patched.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import errno
import json
import os
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from threading import Event, Thread
import time
from typing import Any, Mapping

import pytest

from coat_sim.core import film_synthesis_parallel as parallel
from coat_sim.core.film_auto_synthesis import (
    FilmSynthesisResult, _proxy, freeze_film_synthesis_request, synthesize_film_design,
)
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_structure import (
    _insert_at_site, _site_records, evaluate_frozen_structure_candidate,
)
from coat_sim.core.film_synthesis_io import (
    film_synthesis_request_from_payload, film_synthesis_request_to_payload,
    film_synthesis_result_to_payload,
)
from coat_sim.core.film_synthesis_jobs import (
    build_job_payload, canonical_parameters, execute_job, validate_checkpoint,
)
from coat_sim.core.process_identity import _process_identity
from coat_sim.scheduler.io import write_json
from tests.test_film_auto_synthesis import setup_problem

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


@pytest.fixture
def real_pool(monkeypatch):
    if parallel.available_cpu_workers() < 2:
        pytest.skip("requires two actually available CPU workers")
    monkeypatch.setattr(parallel, "MIN_PARALLEL_WORK", 0)


def _problem(*, workers=1, cap=None, unknown=False):
    service, request = setup_problem(upper=.005, low=520., high=580., samples=5)
    environment = deepcopy(dict(request.environment))
    if unknown:
        service.materials.records["glass"]["optical_sets"][0]["k"] = None
        environment["assume_lossless"] = True
    request = replace(request, environment=environment, cpu_workers=workers,
        max_cycles=1, max_evaluations=cap,
        growth_quarter_wave_fractions=(.5, 1., 1.5, 2.))
    return service, freeze_film_synthesis_request(service, request)


def _assert_equivalent(serial: FilmSynthesisResult, actual: FilmSynthesisResult):
    for key in ("state", "termination_reason", "targets_met", "candidate_design",
                "cycles", "evaluations", "validation_evaluations", "failed_evaluations",
                "validation_complete", "audit_physical_evaluations", "audit_wavelength_points"):
        assert getattr(actual, key) == getattr(serial, key), key
    assert [asdict(row) for row in actual.trace] == [asdict(row) for row in serial.trace]
    assert actual.training_score == serial.training_score
    assert actual.validation_score == serial.validation_score
    assert actual.frozen_request.frozen_material_snapshot == serial.frozen_request.frozen_material_snapshot


def _assert_closed(performance: Mapping[str, Any], *, require_workers=False):
    assert performance["execution_state"] == "closed"
    assert performance["cleanup_complete"] is True
    assert performance["active_worker_pids"] == []
    workers = performance["workers"]
    if require_workers:
        assert len(workers) >= 2
    owned: list[tuple[int, int | None]] = []
    for worker in workers:
        assert worker["qt_imported"] is False
        assert worker["actual_worker_pid"] in performance["worker_pids"]
        assert worker["wrapper_pid"] in performance["launcher_pids"]
        for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "BLIS_NUM_THREADS"):
            assert worker["thread_environment"][name] == "1"
        owned.extend(((worker["actual_worker_pid"], worker.get("creation_time")),
                      (worker["wrapper_pid"], worker.get("wrapper_creation_time"))))
    deadline = time.monotonic() + 3.
    while True:
        remaining = []
        for pid, stamp in owned:
            status, identity = _process_identity(pid)
            assert status != "unknown", (pid, status)
            if status == "alive" and (os.name != "nt" or identity == f"windows:{stamp}"):
                remaining.append(pid)
        if not remaining:
            return
        assert time.monotonic() < deadline, ("owned process still alive", remaining)
        time.sleep(.02)


@pytest.mark.parametrize("workers", [2, 4])
def test_raw_growth_parallel_keeps_current_serial_decisions_and_actual_work(real_pool, workers):
    _service, request = _problem()
    serial = synthesize_film_design(None, request)
    actual = synthesize_film_design(None, replace(request, cpu_workers=workers))
    assert serial.state == "completed" and serial.candidate_design is not None
    assert len(serial.candidate_design["front_layers"]) == 1
    _assert_equivalent(serial, actual)
    assert actual.performance["backend"] == "headless_process"
    assert actual.performance["parallel_batches"] >= 1
    assert actual.performance["submitted_tasks"] >= actual.performance["consumed_tasks"]
    assert actual.performance["discarded_tasks"] == actual.performance["submitted_tasks"] - actual.performance["consumed_tasks"]
    assert actual.performance["known_training_evaluator_invocations"] >= serial.performance["known_training_evaluator_invocations"]
    if actual.performance["unknown_tasks"]:
        assert actual.performance["training_evaluator_invocations"] is None
    else:
        assert actual.performance["training_evaluator_invocations"] >= serial.performance["training_evaluator_invocations"]
    if actual.performance["actual_work_complete"]:
        assert actual.performance["training_wavelength_points"] >= serial.performance["training_wavelength_points"]
    else:
        assert actual.performance["training_wavelength_points"] is None
    _assert_closed(actual.performance, require_workers=True)


@pytest.mark.parametrize("cap", [3, 5, 9])
def test_finite_evaluation_cap_keeps_partial_scan_and_growth_serial(real_pool, cap):
    _service, request = _problem(cap=cap)
    serial = synthesize_film_design(None, request)
    actual = synthesize_film_design(None, replace(request, cpu_workers=2))
    _assert_equivalent(serial, actual)
    assert actual.evaluations <= cap
    assert actual.performance["backend"] == "serial"
    assert actual.performance["submitted_tasks"] == 0
    assert actual.performance["worker_pids"] == []
    _assert_closed(actual.performance)


def _tasks(request, count=7):
    for index in range(count):
        design = deepcopy(dict(request.environment))
        design["front_layers"] = [{"id": f"independent-{index}", "material_id": "low",
            "optical_set_id": "nk", "thickness": {"value": 40. + index * 11., "unit": "nm"}, "note": ""}]
        yield parallel.EvaluationTask(design, context=index)


def test_real_zero_needles_preserve_split_host_gradient_and_finite_batch_guard(real_pool):
    _service, request = _problem()
    design = deepcopy(dict(request.environment))
    design["front_layers"] = [{"id": "preserved-host", "material_id": "high", "optical_set_id": "nk",
        "thickness": {"value": 120., "unit": "nm"}, "note": "Original host"}]
    before = deepcopy(design)
    tasks = []
    for index, site in enumerate(_site_records(design, request.frozen_material_snapshot, 1)):
        for material_index, material in enumerate(request.candidate_materials):
            needle = f"independent-needle-{index}-{material_index}"
            zero = _insert_at_site(design, site, material, 0., needle,
                f"independent-split-{index}-{material_index}", zero=True)
            tasks.append(parallel.EvaluationTask(zero, True, (needle,), (index, material_index)))
    assert len(tasks) == 6 and any(len(task.design["front_layers"]) == 3 for task in tasks)
    for allowed in (False, True):
        executor = parallel.OrderedEvaluationExecutor(2)
        executor.configure(_proxy(request))
        observed = []
        try:
            with executor.batch(tasks, count=len(tasks), work=1_000_000, budget_allows=allowed) as batch:
                for task, outcome in batch:
                    observed.append(task.context)
                    def expected(task=task):
                        return evaluate_frozen_structure_candidate(_proxy(request), task.design,
                            variable_ids=tuple(row["id"] for row in task.design["front_layers"]),
                            with_jacobian=True, zero_thickness_layer_ids=task.zero_ids)
                    value = executor.take(outcome) if outcome is not None else executor.serial(expected, task)
                    reference = expected()
                    assert value["gradient_by_layer_id"] == reference["gradient_by_layer_id"]
                    assert value["score"] == reference["score"]
            assert observed == [task.context for task in tasks]
        finally:
            executor.close()
        assert bool(executor.diagnostics()["submitted_tasks"]) is allowed
        _assert_closed(executor.diagnostics(), require_workers=allowed)
    assert design == before


def test_real_worker_physical_error_is_delivered_only_at_its_ordinal(real_pool):
    _service, request = _problem()
    tasks = list(_tasks(request))
    tasks[3].design["front_layers"][0]["thickness"] = {"value": -1., "unit": "nm"}
    executor = parallel.OrderedEvaluationExecutor(2)
    executor.configure(_proxy(request))
    contexts = []
    try:
        with executor.batch(tasks, count=len(tasks), work=1_000_000, budget_allows=True) as batch:
            for task, outcome in batch:
                contexts.append(task.context)
                assert outcome is not None
                if task.context == 3:
                    with pytest.raises(ValueError):
                        executor.take(outcome)
                else:
                    actual = executor.take(outcome)
                    expected = evaluate_frozen_structure_candidate(_proxy(request), task.design,
                        variable_ids=(task.design["front_layers"][0]["id"],), with_jacobian=True)
                    assert actual["score"] == expected["score"]
                    assert actual["gradient_by_layer_id"] == expected["gradient_by_layer_id"]
        assert contexts == list(range(len(tasks)))
    finally:
        executor.close()
    _assert_closed(executor.diagnostics(), require_workers=True)


def test_real_worker_exit_falls_back_without_reordering_already_pending_work(real_pool):
    _service, request = _problem()
    tasks = list(_tasks(request))
    executor = parallel.OrderedEvaluationExecutor(2)
    executor.configure(_proxy(request))
    contexts = []
    try:
        with executor.batch(tasks, count=len(tasks), work=1_000_000, budget_allows=True) as batch:
            # These handles were created by this test's own executor. Ending its
            # first owned process tree injects a real transport failure only.
            child = executor.children[0]
            if child.job is not None:
                child.job.close()
            else:
                child.process.terminate()
            child.process.wait(timeout=3.)
            for task, outcome in batch:
                contexts.append(task.context)
                def expected(task=task):
                    return evaluate_frozen_structure_candidate(_proxy(request), task.design,
                        variable_ids=(task.design["front_layers"][0]["id"],), with_jacobian=True)
                if outcome is None:
                    actual = executor.serial(expected, task)
                else:
                    try:
                        actual = executor.take(outcome)
                    except parallel.InfrastructureFailure:
                        actual = executor.serial(expected, task, fallback=True)
                assert actual["score"] == expected()["score"]
        assert contexts == list(range(len(tasks)))
        # The other worker has finished its command and is idle. An unexpected
        # exit here must also release a writer waiting on its empty queue.
        idle = executor.children[-1]
        if idle.job is not None:
            idle.job.close()
        else:
            idle.process.terminate()
        idle.process.wait(timeout=3.)
    finally:
        executor.close()
    diagnostics = executor.diagnostics()
    try:
        assert diagnostics["fallback_events"] and diagnostics["serial_fallback_tasks"] >= 1
        _assert_closed(diagnostics, require_workers=True)
    finally:
        # Only rescue this test's private idle writer on a failing implementation.
        # The cleanup assertion above remains false and the regression stays red.
        for child in executor.children:
            if child.writer is not None and child.writer.is_alive():
                child.writer_queue.put_nowait({"kind": "close"})
                child.writer.join(timeout=2.)


def test_real_inflight_cancel_closes_actual_workers_and_wrappers_and_keeps_archive(real_pool, monkeypatch):
    service, request = _problem()
    glass = service.materials.records["glass"]["optical_sets"][0]
    glass.update(wavelength_min=500., wavelength_max=600., wavelengths=[500., 550., 600.],
        n=[1.5, 2.5, 1.5])
    request = freeze_film_synthesis_request(service, replace(request,
        objectives=(FilmOptimizationObjective(500., 600., 2, mode="range", upper_bound=.06),),
        frozen_material_snapshot=None))
    event = Event()
    original = parallel.OrderedEvaluationExecutor.take
    consumed = []
    snapshots = []

    def consume(executor, outcome):
        value = original(executor, outcome)
        consumed.append(outcome.token)
        event.set()
        return value

    monkeypatch.setattr(parallel.OrderedEvaluationExecutor, "take", consume)
    result = synthesize_film_design(None, replace(request, cpu_workers=2), cancel_event=event,
        snapshot_callback=lambda *values: snapshots.append((event.is_set(), deepcopy(values[0]))))
    assert consumed
    assert result.state == "cancelled" and result.targets_met is False
    assert snapshots and not any(cancelled for cancelled, _design in snapshots)
    assert result.validation_complete is True
    assert result.candidate_design == snapshots[-1][1]
    payload = film_synthesis_result_to_payload("parallel-cancelled-audited", result)
    assert payload["best_candidate_audit"]["available"] is True
    assert payload["result"]["targets_met"] is False
    assert result.performance["consumed_tasks"] == 1
    assert result.performance["discarded_tasks"] >= 1
    _assert_closed(result.performance, require_workers=True)


def test_frozen_unknown_k_queue_checkpoint_roundtrip_preserves_historical_performance(real_pool, tmp_path, monkeypatch):
    service, request = _problem(workers=2, unknown=True)
    original = json.dumps(service.materials.records, sort_keys=True)
    payload = film_synthesis_request_to_payload("independent-parallel-checkpoint", request)
    _identifier, parsed, structures = film_synthesis_request_from_payload(json.loads(json.dumps(payload)))
    assert not structures and parsed.cpu_workers == 2
    assert parsed.frozen_material_snapshot == request.frozen_material_snapshot
    job = build_job_payload(canonical_parameters(None, {"payload": payload}))
    checkpoint_path = tmp_path / "best-candidate.json"
    response = execute_job({"payload": job}, Event(), lambda _value: None,
        checkpoint_path, write_json, execution_project_root=str(tmp_path))
    assert response["result"]["state"] == "completed"
    performance = response["result"]["performance"]
    _assert_closed(performance, require_workers=True)
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["synthesis_performance"]["recorded"] is True
    assert checkpoint["synthesis_performance"]["submitted_tasks"] > 0
    candidate = response["result"]["candidate_design"]
    path = tmp_path / "parallel-assumed-candidate.cfilm"
    files = FilmDesignService(tmp_path)
    files.save(path, candidate)
    loaded = files.load(path)
    assert loaded["front_layers"] == candidate["front_layers"]
    assert loaded["assume_lossless"] is True and loaded["use_k"] is True
    assert "glass/nk" in loaded["note"] + loaded.get("summary", "")
    assert json.dumps(service.materials.records, sort_keys=True) == original
    assert response["absorption_assumption"]["source_records_unchanged"] is True

    def forbidden(*_args, **_kwargs):
        raise AssertionError("historical checkpoint recovery must not start or own a live pool")

    monkeypatch.setattr(parallel.subprocess, "Popen", forbidden)
    recovered = validate_checkpoint(checkpoint)
    history = recovered["result"]["performance"]
    assert history["recorded"] is True and history["historical"] is True
    assert history["execution_state"] == "historical_checkpoint"
    assert history["active_worker_pids"] == [] and history["actual_work_complete"] is False
    for key in ("submitted_tasks", "consumed_tasks", "training_physical_evaluations"):
        assert history[key] == checkpoint["synthesis_performance"][key]
    assert recovered["result"]["candidate_design"] == candidate
    legacy = deepcopy(checkpoint)
    legacy.pop("synthesis_performance")
    legacy["audit"].pop("synthesis_performance", None)
    missing = validate_checkpoint(legacy)["result"]["performance"]
    assert missing["recorded"] is False and missing["historical"] is True
    assert missing["training_physical_evaluations"] is None
    assert missing["training_wavelength_points"] is None
    assert missing["active_worker_pids"] == []


def _finished_executor(tmp_path, *, children=1):
    """Actual owned subprocess pipes, without optical work or a running reader."""
    executor = parallel.OrderedEvaluationExecutor(1)
    executor.temporary = TemporaryDirectory(prefix="closed-worker-pipe-", dir=tmp_path)
    for _ in range(children):
        stderr = TemporaryFile()
        process = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=stderr)
        process.wait(timeout=10.)
        executor.children.append(parallel._Child(process, stderr, Thread(target=lambda: None)))
    return executor


def _rescue_test_streams(executor):
    # Rescue only our already-finished processes if a failing close leaves streams.
    for child in executor.children:
        for stream in (child.process.stdin, child.process.stdout, child.stderr):
            try:
                stream.close()
            except (OSError, RuntimeError):
                pass
    executor.temporary.cleanup()


def test_close_flushes_buffered_stdin_after_real_owned_reader_has_exited(tmp_path):
    executor = _finished_executor(tmp_path)
    child = executor.children[0]
    temporary_path = executor.temporary.name
    assert child.process.poll() == 0
    assert child.process.stdin.write(b"buffered bytes for the exited owned reader") > 0
    try:
        # POSIX flush raises EPIPE; Windows FileIO reports EINVAL for this exact
        # dead anonymous-pipe case. Both have actually closed the stream.
        executor.close()
        executor.close()
        assert all(stream.closed for stream in (child.process.stdin, child.process.stdout, child.stderr))
        assert not os.path.exists(temporary_path)
        _assert_closed(executor.diagnostics())
    finally:
        _rescue_test_streams(executor)


@pytest.mark.parametrize("error,ignored", [
    (BrokenPipeError(errno.EPIPE, "POSIX closed owned pipe flush"), True),
    (OSError(errno.EBADF, "unrelated bad descriptor"), False),
    (RuntimeError("unexpected close implementation failure"), False),
])
def test_close_error_releases_all_streams_and_only_ignores_dead_pipe(tmp_path, error, ignored):
    executor = _finished_executor(tmp_path, children=2)
    child = executor.children[0]
    original = child.process.stdin
    temporary_path = executor.temporary.name

    class FailedClose:
        @property
        def closed(self):
            return original.closed

        def close(self):
            original.close()
            raise error

    child.process.stdin = FailedClose()
    try:
        if ignored:
            executor.close()
        else:
            with pytest.raises(type(error)) as caught:
                executor.close()
            assert caught.value is error
        # Even a programming error must remain visible after releasing the other
        # owned streams/children and the run's temporary directory.
        assert original.closed
        assert all(row.process.stdout.closed and row.stderr.closed for row in executor.children)
        assert executor.children[1].process.stdin.closed
        assert not os.path.exists(temporary_path)
        _assert_closed(executor.diagnostics())
    finally:
        child.process.stdin = original
        _rescue_test_streams(executor)
