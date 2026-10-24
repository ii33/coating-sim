from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import MethodType
from uuid import uuid4

import numpy as np
import pytest

from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import (
    FilmOptimizationObjective,
    FilmOptimizationRequest,
    FilmThicknessVariable,
    optimize_film_design,
)
from coat_sim.core.film_optimization_forward_cache import create_worker_forward_service
from coat_sim.core.film_optimization_job import (
    execute_film_optimization_job,
    _service_with_frozen_materials,
    serialize_result,
)
from coat_sim.core.film_optimization_solvers import SolverOutcome, SolverStop
from coat_sim.core.film_performance import PerformanceOptions
from coat_sim.scheduler.jobs import canonical, freeze_spec
from coat_sim.scheduler.store import SchedulerStore
from tests.film_performance_fixtures import fixture


ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent


_SOLVERS = (
    "baseline", "powell", "nelder_mead", "multistart",
    "differential_evolution", "simulated_annealing", "trf", "lbfgsb",
)


def _request(design, frozen, *, solver="baseline", samples=3, budget=8):
    return FilmOptimizationRequest(
        design_snapshot=deepcopy(design),
        design_revision=29,
        variables=(FilmThicknessVariable("front", "layer-0", 45.0, 120.0),),
        objectives=(FilmOptimizationObjective(500.0, 650.0, samples, pointwise=FilmObjectiveConstraint(mode="value", target_value=0.0, lower_bound=None, upper_bound=None), metric="R", angles_deg=(0.0,), polarization="unpolarized"),),
        max_iterations=2,
        max_evaluations=budget,
        initial_step_fraction=0.25,
        min_step_fraction=1e-4,
        acceptable_normalized_error=0.0,
        frozen_material_snapshot=deepcopy(frozen),
        solver=solver,
        seed=731,
        multistart_count=2,
        population_size=5,
        validation_samples_multiplier=2,
    )


def _assert_checkpoint_traces_match(reference, cached):
    assert len(cached) == len(reference) >= 1
    for expected, actual in zip(reference, cached):
        assert set(actual) == set(expected)
        for key in ("schema_version", "kind", "design_revision", "iterations", "evaluations", "targets_met"):
            assert actual[key] == expected[key]
        for key in ("max_normalized_error", "weighted_max_normalized_error", "weighted_rms_error"):
            assert actual[key] == pytest.approx(expected[key], rel=2e-11, abs=2e-13)
        assert actual["best_thickness_nm"].keys() == expected["best_thickness_nm"].keys()
        for layer_id, value in expected["best_thickness_nm"].items():
            assert actual["best_thickness_nm"][layer_id] == pytest.approx(value, rel=2e-12, abs=2e-12)
        # The queue checkpoint is the public representation of the optimizer's
        # lexicographic archive key; keep its order and winning key unchanged.
        assert (actual["max_normalized_error"], actual["weighted_rms_error"]) == pytest.approx(
            (expected["max_normalized_error"], expected["weighted_rms_error"]),
            rel=2e-11, abs=2e-13)


def _assert_serialized_checkpoints_match(reference, cached):
    """Compare queue checkpoint envelopes returned by the original job API."""
    assert len(cached) == len(reference) >= 1
    for expected, actual in zip(reference, cached):
        assert set(actual) == set(expected)
        assert actual["request"] == expected["request"]
        assert actual["partial_result"] == expected["partial_result"]
        expected_candidate = expected["candidate"]
        actual_candidate = actual["candidate"]
        assert set(actual_candidate) == set(expected_candidate)
        for key in ("schema_version", "kind", "design_revision", "design",
                    "per_condition_errors", "iterations", "evaluations"):
            assert actual_candidate[key] == expected_candidate[key]
        expected_quality = expected_candidate["quality"]
        actual_quality = actual_candidate["quality"]
        assert set(actual_quality) == set(expected_quality)
        for key in ("metric", "direction", "targets_met"):
            assert actual_quality[key] == expected_quality[key]
        for key in ("value", "weighted_max_normalized_error", "weighted_rms_error"):
            assert actual_quality[key] == pytest.approx(
                expected_quality[key], rel=2e-11, abs=2e-13)
        assert actual_candidate["thickness_nm"].keys() == expected_candidate["thickness_nm"].keys()
        for layer_id, value in expected_candidate["thickness_nm"].items():
            assert actual_candidate["thickness_nm"][layer_id] == pytest.approx(
                value, rel=2e-12, abs=2e-12)


def _queue_job_spec(service, design, frozen):
    request = _request(design, frozen, solver="baseline", samples=3, budget=8)
    return freeze_spec(service, "film_optimization", {"request": request})


def test_original_job_entry_keeps_cache_opt_in_and_results_equivalent():
    service, design, frozen = fixture(layers=4)
    spec = _queue_job_spec(service, design, frozen)
    payload = spec["parameters"]["payload"]
    regular_checkpoints, cached_checkpoints, performance = [], [], []

    regular = execute_film_optimization_job(
        service, payload, cancel_event=Event(),
        checkpoint_callback=regular_checkpoints.append,
    )
    cached = execute_film_optimization_job(
        service, payload, cancel_event=Event(),
        checkpoint_callback=cached_checkpoints.append,
        enable_forward_cache=True, performance_callback=performance.append,
    )

    assert regular["state"] == cached["state"] == "completed"
    assert "performance" not in regular and "performance" not in cached
    assert regular["candidate"]["design_revision"] == cached["candidate"]["design_revision"]
    assert regular["candidate"]["thickness_nm"].keys() == cached["candidate"]["thickness_nm"].keys()
    for layer_id, value in regular["candidate"]["thickness_nm"].items():
        assert cached["candidate"]["thickness_nm"][layer_id] == pytest.approx(
            value, rel=2e-11, abs=2e-12)
    for field in ("value", "weighted_max_normalized_error", "weighted_rms_error"):
        assert cached["candidate"]["quality"][field] == pytest.approx(
            regular["candidate"]["quality"][field], rel=2e-11, abs=2e-13)
    _assert_serialized_checkpoints_match(regular_checkpoints, cached_checkpoints)
    assert performance
    assert performance[-1]["actual_backend"] == "cpu"
    assert performance[-1]["dtype"] == "float64/complex128"
    assert performance[-1]["cpu_workers"] == 1
    assert performance[-1]["capacity"] == 2

    with pytest.raises(TypeError, match="enable_forward_cache"):
        execute_film_optimization_job(
            service, payload, cancel_event=Event(), enable_forward_cache=1)


def test_real_scheduler_worker_cache_opt_in_preserves_checkpoint_and_sidecar(tmp_path):
    service, design, frozen = fixture(layers=4)
    spec = _queue_job_spec(service, design, frozen)
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
    env_base = dict(os.environ)
    env_base["PYTHONPATH"] = os.pathsep.join(filter(
        None, (str(ROOT), str(TESTS), env_base.get("PYTHONPATH", ""))))
    outputs = {}
    checkpoints = {}
    for enabled in (False, True):
        root = tmp_path / ("cache-enabled" if enabled else "cache-default")
        root.mkdir()
        job_id = uuid4().hex
        owner_instance = uuid4().hex
        directory = root / "artifacts/runtime/scheduler/jobs" / job_id
        directory.mkdir(parents=True)
        SchedulerStore(root).submit(spec, job_id=job_id)
        (directory / "spec.json").write_text(canonical(spec), encoding="utf-8")
        (directory / "launch.json").write_text(
            json.dumps({"instance": owner_instance}), encoding="utf-8")
        env = dict(env_base)
        if enabled:
            env["COAT_SIM_FILM_FORWARD_CACHE"] = "1"
        else:
            env.pop("COAT_SIM_FILM_FORWARD_CACHE", None)

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
        assert "performance" not in result
        outputs[enabled] = result
        checkpoint = json.loads(
            (directory / "best-candidate.json").read_text(encoding="utf-8"))
        assert "performance" not in checkpoint["result"]
        checkpoints[enabled] = checkpoint["result"]

        sidecar = directory / "film-performance.json"
        if enabled:
            metrics = json.loads(sidecar.read_text(encoding="utf-8"))
            assert metrics["actual_backend"] == "cpu"
            assert metrics["dtype"] == "float64/complex128"
            assert metrics["cpu_workers"] == 1
            assert metrics["capacity"] == 2
            progress = json.loads((directory / "progress.json").read_text(encoding="utf-8"))
            assert progress["stage"] in {"film_forward_cache", "film_optimization"}
            assert progress["actual_backend"] == "cpu"
            assert progress["dtype"] == "float64/complex128"
            assert progress["forward_calls"] >= 1
        else:
            assert not sidecar.exists()

    regular = outputs[False]
    cached = outputs[True]
    assert regular["state"] == cached["state"]
    assert regular["candidate"]["thickness_nm"].keys() == cached["candidate"]["thickness_nm"].keys()
    for layer_id, value in regular["candidate"]["thickness_nm"].items():
        assert cached["candidate"]["thickness_nm"][layer_id] == pytest.approx(
            value, rel=2e-11, abs=2e-12)
    for field in ("value", "weighted_max_normalized_error", "weighted_rms_error"):
        assert cached["candidate"]["quality"][field] == pytest.approx(
            regular["candidate"]["quality"][field], rel=2e-11, abs=2e-13)
    _assert_serialized_checkpoints_match([checkpoints[False]], [checkpoints[True]])


@pytest.mark.parametrize("solver", _SOLVERS)
def test_forward_cache_preserves_real_optimizer_semantics_for_all_methods(solver):
    service, design, frozen = fixture(layers=4)
    baseline_service = _service_with_frozen_materials(service, frozen)
    cached_service = create_worker_forward_service(baseline_service, frozen)
    request = _request(design, frozen, solver=solver, budget=8)
    expected_checkpoints, actual_checkpoints = [], []

    expected = optimize_film_design(
        baseline_service, request, checkpoint_callback=expected_checkpoints.append)
    actual = optimize_film_design(
        cached_service, request, checkpoint_callback=actual_checkpoints.append)

    assert actual.solver == expected.solver == solver
    assert actual.seed == expected.seed == request.seed
    assert actual.evaluations == expected.evaluations <= request.max_evaluations
    assert actual.iterations == expected.iterations <= request.max_iterations
    assert actual.termination_reason == expected.termination_reason
    assert actual.state == expected.state
    assert actual.best_thickness_nm.keys() == expected.best_thickness_nm.keys()
    for layer_id, value in expected.best_thickness_nm.items():
        assert actual.best_thickness_nm[layer_id] == pytest.approx(value, rel=2e-11, abs=2e-12)
    for field in ("max_normalized_error", "weighted_max_normalized_error", "weighted_rms_error"):
        assert getattr(actual, field) == pytest.approx(getattr(expected, field), rel=2e-11, abs=2e-13)
    assert len(actual.per_condition_errors) == len(expected.per_condition_errors)
    for expected_error, actual_error in zip(expected.per_condition_errors, actual.per_condition_errors):
        assert actual_error.objective_index == expected_error.objective_index
        assert actual_error.wavelength_nm == expected_error.wavelength_nm
        assert actual_error.actual == pytest.approx(expected_error.actual, rel=2e-11, abs=2e-13)
        assert actual_error.normalized_violation == pytest.approx(
            expected_error.normalized_violation, rel=2e-11, abs=2e-13)
    _assert_checkpoint_traces_match(expected_checkpoints, actual_checkpoints)

    # Operational diagnostics stay on the worker callback, outside queue JSON.
    result_payload = serialize_result(request, actual)
    assert "performance" not in result_payload
    json.dumps(result_payload, allow_nan=False)


def test_small_medium_cache_is_single_cpu_bounded_and_returns_plain_spectra():
    service, design, frozen = fixture(layers=6)
    snapshots = []
    worker = create_worker_forward_service(
        _service_with_frozen_materials(service, frozen), frozen,
        options=PerformanceOptions(
            backend="cpu", cpu_workers=4, max_prepared_bytes=1024 * 1024,
            max_result_bytes=256 * 1024),
        performance_callback=snapshots.append,
    )
    first = deepcopy(design)
    second = deepcopy(first)
    second["front_layers"][0]["thickness"] = {"value": 83.0, "unit": "nm"}
    settings = {
        "wavelength_min_nm": 400.0, "wavelength_max_nm": 800.0,
        "samples": 256, "angle_deg": 0.0, "polarization": "unpolarized",
    }

    expected = service.simulate(first, settings)
    got_first = worker.simulate(first, settings)
    got_second = worker.simulate(second, settings)

    assert set(got_first) == {"wavelength_nm", "R", "T", "A"}
    for channel in ("wavelength_nm", "R", "T", "A"):
        np.testing.assert_allclose(got_first[channel], expected[channel], rtol=2e-11, atol=2e-13)
    assert len(got_second["R"]) == settings["samples"]
    stats = snapshots[-1]
    assert stats["actual_backend"] == "cpu"
    assert stats["cpu_workers"] == 1
    assert stats["capacity"] == 2
    assert stats["prepared_builds"] == 1
    assert stats["cached_forward_calls"] == 2
    assert stats["cache_hits"] == 1
    assert stats["fallback_calls"] == 0
    assert len(worker._film_forward_cache_adapter.cache) <= 2


def test_canonical_forward_uses_the_frozen_snapshot_after_live_catalog_changes():
    service, design, frozen = fixture(layers=4)
    settings = {"wavelength_min_nm": 500.0, "wavelength_max_nm": 600.0,
                "samples": 5, "angle_deg": 0.0}
    expected = service.simulate(design, settings)
    worker = create_worker_forward_service(service, frozen)

    class ForbiddenCatalog:
        def get(self, material_id):
            raise AssertionError(f"worker consulted the live catalog: {material_id}")

        def _records(self, material_ids=None):
            raise AssertionError("worker enumerated the live catalog")

    service.materials = ForbiddenCatalog()
    actual = worker.simulate(design, settings)

    np.testing.assert_allclose(actual["R"], expected["R"], rtol=2e-11, atol=2e-13)
    np.testing.assert_allclose(actual["T"], expected["T"], rtol=2e-11, atol=2e-13)
    assert worker._film_forward_cache_adapter.stats["fallback_calls"] == 0


def test_configured_settings_lru_evicts_after_two_prepared_grids():
    service, design, frozen = fixture(layers=4)
    states = []
    worker = create_worker_forward_service(
        _service_with_frozen_materials(service, frozen), frozen,
        performance_callback=states.append,
    )
    base = {"wavelength_min_nm": 450.0, "wavelength_max_nm": 700.0,
            "samples": 5, "angle_deg": 0.0, "polarization": "s"}
    for changes in ({}, {"polarization": "p"}, {"angle_deg": 10.0}, {}):
        worker.simulate(design, {**base, **changes})

    stats = states[-1]
    assert stats["prepared_builds"] == 4
    assert stats["cache_evictions"] == 2
    assert stats["cache_hits"] == 0
    assert len(worker._film_forward_cache_adapter.cache) == 2


@pytest.mark.parametrize("case", ["cached", "prepared_size_fallback", "incoherent_fallback"])
def test_single_wavelength_settings_work_for_cache_and_cpu_fallbacks(case):
    if case == "incoherent_fallback":
        service, design, frozen = fixture(double=True, substrate_nm=1_000_000.0)
        options = None
    elif case == "prepared_size_fallback":
        service, design, frozen = fixture(layers=6)
        options = PerformanceOptions(
            backend="cpu", max_prepared_bytes=1024, max_result_bytes=1024)
    else:
        service, design, frozen = fixture(layers=4)
        options = None

    states = []
    worker = create_worker_forward_service(
        _service_with_frozen_materials(service, frozen), frozen,
        options=options, performance_callback=states.append)
    # These are the raw settings produced by the optimizer for a valid
    # single-wavelength objective. A normalized step_nm=0 must not be fed
    # through settings validation a second time by either execution path.
    settings = {
        "wavelength_min_nm": 550.0, "wavelength_max_nm": 550.0,
        "samples": 1, "polarization": "unpolarized",
        "first_surface": "front", "angle_deg": 0.0,
    }

    expected = service.simulate(design, settings)
    actual = worker.simulate(design, settings)

    for channel in ("wavelength_nm", "R", "T", "A"):
        np.testing.assert_allclose(actual[channel], expected[channel], rtol=2e-11, atol=2e-13)
    stats = states[-1]
    assert stats["actual_backend"] == "cpu"
    if case == "cached":
        assert stats["cached_forward_calls"] == 1
        assert stats["prepared_builds"] == 1
        assert stats["fallback_calls"] == 0
    else:
        assert stats["cached_forward_calls"] == 0
        assert stats["prepared_builds"] == 0
        assert stats["fallback_calls"] == 1
        expected_reason = "incoherent" if case == "incoherent_fallback" else "resource limit"
        assert any(expected_reason in reason for reason in stats["fallback_reasons"])


def test_incoherent_and_oversize_inputs_fall_back_before_preparation():
    service, design, frozen = fixture(double=True, substrate_nm=1_000_000.0)
    states = []
    worker = create_worker_forward_service(
        _service_with_frozen_materials(service, frozen), frozen,
        performance_callback=states.append,
    )
    settings = {"wavelength_min_nm": 500.0, "wavelength_max_nm": 600.0,
                "samples": 5, "angle_deg": 0.0}

    expected = service.simulate(design, settings)
    actual = worker.simulate(design, settings)
    np.testing.assert_allclose(actual["R"], expected["R"], rtol=0, atol=1e-12)
    stats = states[-1]
    assert stats["fallback_calls"] == 1
    assert stats["prepared_builds"] == 0
    assert any("incoherent" in reason for reason in stats["fallback_reasons"])

    service2, design2, frozen2 = fixture(layers=6)
    states2 = []
    limited = create_worker_forward_service(
        _service_with_frozen_materials(service2, frozen2), frozen2,
        options=PerformanceOptions(
            backend="cpu", max_prepared_bytes=1024, max_result_bytes=1024),
        performance_callback=states2.append,
    )
    large_grid = {"wavelength_min_nm": 400.0, "wavelength_max_nm": 800.0,
                  "samples": 64, "angle_deg": 0.0}
    expected2 = service2.simulate(design2, large_grid)
    actual2 = limited.simulate(design2, large_grid)
    np.testing.assert_allclose(actual2["R"], expected2["R"], rtol=0, atol=1e-12)
    stats2 = states2[-1]
    assert stats2["fallback_calls"] == 1
    assert stats2["prepared_builds"] == 0
    assert any("resource limit" in reason for reason in stats2["fallback_reasons"])


def test_custom_simulate_is_called_and_its_behavior_is_not_replaced():
    from coat_sim.core.film_design_service import FilmDesignService

    service, design, frozen = fixture(layers=4)
    calls = []

    def custom_simulate(self, candidate, settings=None, *, cancel_event=None):
        calls.append((candidate["front_layers"][0]["thickness"]["value"], settings))
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("custom cancellation")
        output = FilmDesignService.simulate(self, candidate, settings, cancel_event=cancel_event)
        output["R"] = [value + 0.125 for value in output["R"]]
        return output

    service.simulate = MethodType(custom_simulate, service)
    settings = {"wavelength_min_nm": 500.0, "wavelength_max_nm": 600.0,
                "samples": 3, "angle_deg": 0.0}
    expected = service.simulate(design, settings)
    states = []
    worker = create_worker_forward_service(service, frozen, performance_callback=states.append)

    class ForbiddenCatalog:
        def get(self, material_id):
            raise AssertionError(f"fallback consulted the live catalog: {material_id}")

        def _records(self, material_ids=None):
            raise AssertionError("fallback enumerated the live catalog")

    service.materials = ForbiddenCatalog()
    calls.clear()
    actual = worker.simulate(design, settings)

    np.testing.assert_allclose(actual["R"], expected["R"], rtol=0, atol=1e-12)
    assert len(calls) == 1
    assert states[-1]["fallback_calls"] == 1
    assert states[-1]["prepared_builds"] == 0
    assert any("custom simulate" in reason for reason in states[-1]["fallback_reasons"])

    cancel = Event()
    cancel.set()
    with pytest.raises(InterruptedError, match="custom cancellation"):
        worker.simulate(design, settings, cancel_event=cancel)
    assert states[-1]["fallback_calls"] == 2
    assert states[-1]["cancelled_forward_calls"] == 1
    assert states[-1]["failed_forward_calls"] == 0


def test_topology_change_falls_back_without_preparing_a_new_stack():
    service, design, frozen = fixture(layers=4)
    states = []
    worker = create_worker_forward_service(
        _service_with_frozen_materials(service, frozen), frozen,
        performance_callback=states.append,
    )
    settings = {"wavelength_min_nm": 500.0, "wavelength_max_nm": 600.0,
                "samples": 3, "angle_deg": 0.0}
    worker.simulate(design, settings)
    assert states[-1]["prepared_builds"] == 1

    changed_material = deepcopy(design)
    changed_material["front_layers"][0]["material_id"] = "high"
    expected = service.simulate(changed_material, settings)
    actual = worker.simulate(changed_material, settings)
    np.testing.assert_allclose(actual["R"], expected["R"], rtol=0, atol=1e-12)
    assert states[-1]["fallback_calls"] == 1
    assert states[-1]["prepared_builds"] == 1
    assert any("topology" in reason for reason in states[-1]["fallback_reasons"])

    changed_use_k = deepcopy(design)
    changed_use_k["use_k"] = False
    expected2 = service.simulate(changed_use_k, settings)
    actual2 = worker.simulate(changed_use_k, settings)
    np.testing.assert_allclose(actual2["R"], expected2["R"], rtol=0, atol=1e-12)
    assert states[-1]["fallback_calls"] == 2
    assert states[-1]["prepared_builds"] == 1


def _rehash_snapshot(snapshot, design):
    payload = deepcopy(snapshot)
    payload.pop("sha256", None)
    payload["design"] = deepcopy(design)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    payload["sha256"] = sha256(encoded).hexdigest()
    return payload


def test_quarter_wave_forward_and_extrapolation_policy_are_preserved():
    service, design, frozen = fixture(layers=4)
    design["front_layers"][0]["thickness"] = {"value": 0.25, "unit": "qw"}
    qw_snapshot = _rehash_snapshot(frozen, design)
    worker = create_worker_forward_service(
        _service_with_frozen_materials(service, qw_snapshot), qw_snapshot)
    candidate = deepcopy(design)
    candidate["front_layers"][0]["thickness"]["value"] = 0.4
    settings = {"wavelength_min_nm": 500.0, "wavelength_max_nm": 600.0,
                "samples": 5, "angle_deg": 0.0, "polarization": "p"}
    expected = service.simulate(candidate, settings)
    actual = worker.simulate(candidate, settings)
    np.testing.assert_allclose(actual["R"], expected["R"], rtol=2e-11, atol=2e-13)
    np.testing.assert_allclose(actual["T"], expected["T"], rtol=2e-11, atol=2e-13)
    assert worker._film_forward_cache_adapter.stats["prepared_builds"] == 1

    ordinary_service, ordinary_design, ordinary_frozen = fixture(layers=4)
    policy_states = []
    policy_worker = create_worker_forward_service(
        _service_with_frozen_materials(ordinary_service, ordinary_frozen), ordinary_frozen,
        performance_callback=policy_states.append)
    covered = {"wavelength_min_nm": 400.0, "wavelength_max_nm": 700.0,
               "samples": 5, "angle_deg": 0.0, "extrapolation_policy": "endpoint_hold"}
    policy_worker.simulate(ordinary_design, covered)
    policy_worker.simulate(ordinary_design, {**covered, "extrapolation_policy": "error"})
    assert policy_states[-1]["prepared_builds"] == 2
    assert policy_states[-1]["cache_evictions"] == 0

    outside = {"wavelength_min_nm": 300.0, "wavelength_max_nm": 400.0,
               "samples": 3, "angle_deg": 0.0, "extrapolation_policy": "error"}
    with pytest.raises(ValueError):
        policy_worker.simulate(ordinary_design, outside)
    assert policy_states[-1]["fallback_calls"] == 1
    assert policy_states[-1]["failed_forward_calls"] == 1
    assert policy_states[-1]["prepared_builds"] == 2
    assert any("prepared setup" in reason for reason in policy_states[-1]["fallback_reasons"])


def test_cancel_before_search_keeps_exactly_one_complete_baseline_candidate():
    service, design, frozen = fixture(layers=4)
    frozen_service = _service_with_frozen_materials(service, frozen)
    worker = create_worker_forward_service(frozen_service, frozen)
    request = _request(design, frozen, solver="nelder_mead", budget=8)
    cancel = Event()
    cancel.set()
    checkpoints = []

    result = optimize_film_design(
        worker, request, cancel_event=cancel, checkpoint_callback=checkpoints.append)

    assert result.state == "cancelled"
    assert result.termination_reason == "cancelled"
    assert result.evaluations == 1
    assert result.iterations == 0
    assert result.validation_evaluations == 0
    assert len(checkpoints) == 1
    assert checkpoints[0]["evaluations"] == 1
    assert checkpoints[0]["best_design"] == result.best_design


def test_cancel_after_new_checkpoint_retains_last_complete_incumbent(monkeypatch):
    from coat_sim.core import film_optimization_solvers

    service, design, frozen = fixture(layers=4)
    worker = create_worker_forward_service(
        _service_with_frozen_materials(service, frozen), frozen)
    # Target the actual canonical spectrum at 100 nm so the proposed candidate
    # is a strict incumbent improvement after the complete baseline evaluation.
    probe = deepcopy(design)
    probe["front_layers"][0]["thickness"] = {"value": 100.0, "unit": "nm"}
    spectrum = service.simulate(probe, {"wavelength_min_nm": 550.0,
                                        "wavelength_max_nm": 550.0, "samples": 1,
                                        "angle_deg": 0.0})
    request = _request(design, frozen, solver="nelder_mead", samples=1, budget=8)
    request = FilmOptimizationRequest(
        **{**request.__dict__, "objectives": (FilmOptimizationObjective(550.0, 550.0, 1, pointwise=FilmObjectiveConstraint(mode="value", target_value=float(spectrum["R"][0]), lower_bound=None, upper_bound=None), metric="R", angles_deg=(0.0,)),)})
    cancel = Event()
    checkpoints = []

    def save(checkpoint):
        checkpoints.append(deepcopy(checkpoint))
        if len(checkpoints) == 2:
            cancel.set()

    def propose_then_observe_cancel(method, initial, lower, upper, evaluate, **kwargs):
        del method, initial, lower, upper, kwargs
        try:
            evaluate(np.array([55.0 / 75.0]))  # 45 + 75*x = 100 nm
        except SolverStop as stop:
            return film_optimization_solvers.SolverOutcome(iterations=1, reason=stop.reason)
        return film_optimization_solvers.SolverOutcome(iterations=1, reason="converged")

    monkeypatch.setattr(film_optimization_solvers, "run_solver", propose_then_observe_cancel)
    result = optimize_film_design(
        worker, request, cancel_event=cancel, checkpoint_callback=save)

    assert result.state == "cancelled"
    assert result.termination_reason == "cancelled"
    assert result.evaluations == 2
    assert len(checkpoints) == 2
    incumbent = checkpoints[-1]
    assert incumbent["evaluations"] == 2
    assert incumbent["best_thickness_nm"]["layer-0"] == pytest.approx(100.0)
    assert incumbent["best_design"] == result.best_design
    assert incumbent["max_normalized_error"] == pytest.approx(result.max_normalized_error)

