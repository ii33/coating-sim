"""Desktop selection, queue compatibility and opt-in real Intel execution."""

from concurrent.futures import Future
from copy import deepcopy
import json
import os
from dataclasses import replace
from uuid import uuid4
import time
from typing import Any, cast

import pytest
from PySide6.QtCore import QMetaObject, QObject, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
import shiboken6

from coat_sim.scheduler.client import SchedulerClient
from coat_sim.ui.compute_target import ComputeTarget, ComputeTargetError
from coat_sim.ui.settings import apply_font_settings
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer, window_runtime
from tests.test_film_xpu_execution import BINDING


pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr("coat_sim.ui.settings.load_ui_settings", lambda: {})
    application = (
        cast(QGuiApplication, QGuiApplication.instance())
        if QGuiApplication.instance()
        else QGuiApplication([])
    )
    apply_font_settings({}, application)
    return application


def spin(app, predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate(), "Qt operation did not reach the expected state"


def rows():
    return ComputeTarget._film_cpu_devices() + [
        dict(
            id="xpu:0",
            label="Intel test",
            backend="xpu",
            available=True,
            reason="",
            physical_id=BINDING["uuid"],
            execution_device=deepcopy(BINDING),
        )
    ]


def deliver(target, generation=None):
    future: Future[tuple[int, list[dict[str, Any]], None, str]] = Future()
    future.set_result(
        (target._generation if generation is None else generation, rows(), None, "film_xpu")
    )
    target._film_probe_future = future
    target._film_probe_busy = True
    target._finish_film_probe()


def test_cpu_batch_binding_rejects_gpu_selection_across_context_changes(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        target = cast(ComputeTarget, controller.computeTarget)
        assert target.deviceId == "cpu:default" and target.canSubmit
        assert not controller.deferredBatchEnabled
        assert controller._build_request().execution_device is None
        controller.setSolver("differential_evolution")
        assert controller.setDeferredBatchEnabled(True)
        assert controller.setFilmCpuWorkers(min(2, controller.filmCpuWorkerLimit))
        assert controller.setFilmCpuAxis("wavelengths")
        # Finite-domain differential evolution has no implicit thickness upper;
        # give every selected layer an explicit finite bound before building it.
        for row in cast(list[dict[str, Any]], controller.variables):
            if row["enabled"]:
                assert controller.setVariableBound(
                    row["side"], row["layer_id"], "upper_nm", 400.0)
        request = controller._build_request()
        assert request.execution_device["cpu_parallel_axis"] == "wavelengths"
        # Even if a stale probe delivers a GPU row, the optimizer target cannot
        # select it and every subsequent request remains explicitly CPU-bound.
        deliver(target)
        target.setDeviceId("xpu:0")
        assert target.canSubmit and target.deviceId == "cpu:default"
        assert controller._build_request().execution_device == request.execution_device
        assert controller.setObjectiveField("angle_max_deg", 20.0)
        assert target.canSubmit and target.deviceId == "cpu:default"
        assert controller._build_request().execution_device == request.execution_device


def test_stale_probe_does_not_restore_device_and_allows_another_probe(app, tmp_path):
    target = ComputeTarget(tmp_path, "film_optimization")
    try:
        target.configure_film_context("", True)
        deliver(target)
        target.setDeviceId("xpu:0")
        target.refresh()
        deliver(target, generation=target._generation - 1)
        assert not target.filmXpuProbeBusy
        assert target.devices == target._film_cpu_devices()
        assert not target.canSubmit and target.deviceId == "xpu:0"
        target.setDeviceId("cpu:default")
        assert target.canSubmit
    finally:
        target._executor.shutdown(wait=True, cancel_futures=True)
        shiboken6.delete(target)


def test_old_queue_rejects_new_batch_without_stopping_it(app, tmp_path):
    class Old:
        def __init__(self, _root):
            pass

        def capabilities(self):
            return {"protocol_version": 1, "executors": ["film_optimization"]}

    target = ComputeTarget(tmp_path, "film_optimization", client_factory=Old)
    try:
        target.configure_film_context("", True)
        client, device = target.client_for_submit()
        verify, _current = target.admission_verifier(client, device)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(ComputeTargetError, match="后台尚未支持"):
                pool.submit(verify).result()
        target.configure_film_context("", False)
        assert isinstance(target.client_for_submit()[0], Old)
    finally:
        target._executor.shutdown(wait=True, cancel_futures=True)
        shiboken6.delete(target)


def test_batch_widgets_load_at_narrow_width_and_cpu_settings_remain_explicit(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        controller.setSolver("differential_evolution")
        controller.setDeferredBatchEnabled(True)
        with window_runtime(app, controller) as (window, warnings):
            window.setWidth(740)
            window.setHeight(620)
            assert QMetaObject.invokeMethod(
                window.findChild(QObject, "filmOptimizationAlgorithmTab"),
                "click",
                Qt.ConnectionType.DirectConnection,
            )
            QTest.qWait(40)
            assert window.findChild(QObject, "filmDeferredBatchMode") is not None
            assert window.findChild(QObject, "filmBatchCpuWorkers") is not None
            assert window.findChild(QObject, "computeFilmXpuProbeButton") is None
            assert window.findChild(QObject, "filmOptimizationComputeTarget") is None
            assert not warnings


@pytest.mark.parametrize("operation", ["complete", "cancel", "edited"])
def test_cpu_ui_to_queue_result_and_lifecycle(app, tmp_path, monkeypatch, operation):
    # A GPU-oriented process environment must not override the UI's explicit CPU binding.
    monkeypatch.setenv("COAT_SIM_FILM_BACKEND", "xpu")
    service, _library, design = fixture(tmp_path)
    client = SchedulerClient(tmp_path)
    assert not client.endpoint.exists(), "test must own a fresh queue root"
    try:
        with optimizer(service, design) as (controller, source, _session):
            assert controller.setSolver("differential_evolution")
            assert controller.setDeferredBatchEnabled(True)
            for row in cast(list[dict[str, Any]], controller.variables):
                if row["enabled"]:
                    assert controller.setVariableBound(row["side"], row["layer_id"], "upper_nm", 400.0)
            assert controller.setObjectiveField("wavelength_samples", 101)
            assert controller.setObjectiveField("pointwise.lower_bound_percent", 0.0)
            assert controller.setObjectiveField("pointwise.upper_bound_percent", 0.0)
            controller.setAcceptableErrorPercent(0.0)
            controller.setSolverOption("max_iterations", 3)
            controller.setSolverOption("max_evaluations", 30)
            controller.setSolverOption("population_size", 5)
            if operation == "cancel":
                controller.setSolverOption("max_iterations", 1000)
                controller.setSolverOption("max_evaluations", 100000)
                controller.setSolverOption("population_size", 32)
                controller.setObjectiveField("wavelength_samples", 1001)
            target = cast(ComputeTarget, controller.computeTarget)
            target.requestFilmXpuProbe()
            assert not target.filmXpuProbeBusy
            target.setDeviceId("xpu:0")
            assert target.deviceId == "cpu:default" and target.canSubmit
            assert controller.startOptimization(), controller.error
            job_id = controller._queue_job_id
            assert job_id and controller._frozen_request.execution_device["kind"] == "film_cpu_v1"
            if operation == "cancel":
                def cpu_forward_started():
                    try:
                        return client.status(job_id).get("progress", {}).get("actual_backend") == "cpu"
                    except ValueError as exc:
                        if "job_not_found" not in str(exc):
                            raise
                        return False  # Admission runs asynchronously.
                spin(app, cpu_forward_started, 60)
                controller.cancelOptimization()
            if operation == "edited":
                source.pendingEditEpoch += 1
                source.pendingEditEpochChanged.emit()
            spin(app, lambda: not controller.busy, 90)
            status = client.status(job_id)
            directory = tmp_path / "artifacts/runtime/scheduler/jobs" / job_id
            if operation == "cancel":
                assert status["state"] == "cancelled", status
                assert not source.applied and not controller.canApply
                return
            assert status["state"] == "succeeded", status
            outcome = json.loads((directory / "outcome.json").read_text(encoding="utf-8"))
            performance = json.loads((directory / "film-performance.json").read_text(encoding="utf-8"))
            assert performance["actual_backend"] == "cpu"
            assert outcome["result"]["execution"]["forward_backend"] == "cpu"
            assert outcome["result"]["candidate"]["evaluations"] > 1
            assert outcome["result"]["solver_validation"]["validation_complete"] is True
            from coat_sim.scheduler.result_capability import validate_film_optimization_result
            assert validate_film_optimization_result(service, outcome["result"]) == outcome["result"]
            assert not controller.error if operation == "complete" else "编辑" in controller.error
            assert bool(source.applied) == (operation == "complete")
            assert controller._result.validation_complete
    finally:
        if client.endpoint.exists():
            client._call("stop", start=False)  # Only this test's unique, previously absent queue.
            process = client._processes.get(str(client.root))
            if process is not None:
                process.wait(timeout=15)


def _native_xpu_spec(service, design, binding, operation):
    from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
    from coat_sim.core.film_optimization import FilmOptimizationObjective
    from coat_sim.scheduler.jobs import freeze_spec
    from tests.film_structure_fixtures import request as make_request

    objective = FilmOptimizationObjective(
        wavelength_min_nm=500.0, wavelength_max_nm=700.0,
        wavelength_samples=1001 if operation == "cancel" else 101,
        angles_deg=(0.0,), polarization="unpolarized", metric="R",
        pointwise=FilmObjectiveConstraint(mode="value", target_value=0.0,
                                         lower_bound=None, upper_bound=None))
    request = make_request(design, [objective], bounds=(0.0, 400.0),
                           max_iterations=1000 if operation == "cancel" else 3,
                           max_evaluations=100000 if operation == "cancel" else 30)
    request = replace(request, solver="differential_evolution", execution_mode="deferred_batch",
                      execution_device=binding, population_size=32 if operation == "cancel" else 5,
                      acceptable_normalized_error=0.0)
    return freeze_spec(service, "film_optimization", {"request": request})


def test_native_xpu_scheduler_spec_is_constructible_without_hardware(tmp_path):
    service, _library, design = fixture(tmp_path)
    for operation in ("complete", "cancel", "stale_uuid"):
        spec = _native_xpu_spec(service, design, BINDING, operation)
        request = spec["parameters"]["payload"]["request"]
        assert request["execution_device"] == BINDING
        assert request["execution_mode"] == "deferred_batch"


@pytest.mark.real_gpu
@pytest.mark.skipif(
    os.environ.get("COAT_SIM_TEST_XPU") != "1", reason="explicit native Intel XPU run only"
)
@pytest.mark.parametrize("operation", ["complete", "cancel", "stale_uuid"])
def test_native_xpu_direct_scheduler_binding_and_lifecycle(tmp_path, operation):
    """Exercise real XPU after the optimizer window's selector was retired."""
    from coat_sim.core.film_xpu_execution import probe_devices

    service, _library, design = fixture(tmp_path)
    device = probe_devices()[0]["execution_device"]
    binding = deepcopy(device)
    if operation == "stale_uuid":
        binding["uuid"] = ("00000000-0000-0000-0000-000000000002"
                           if device["uuid"] != "00000000-0000-0000-0000-000000000002"
                           else "00000000-0000-0000-0000-000000000003")
    spec = _native_xpu_spec(service, design, binding, operation)
    client = SchedulerClient(tmp_path)
    assert not client.endpoint.exists(), "test must own a fresh queue root"
    job_id = uuid4().hex
    try:
        capabilities = client.capabilities()
        assert capabilities.get("film_xpu_execution") == 1
        client.submit(spec, job_id=job_id)
        deadline = time.monotonic() + 120
        cancelled = False
        while time.monotonic() < deadline:
            status = client.status(job_id)
            if (operation == "cancel" and not cancelled
                    and status.get("progress", {}).get("gpu_runtime") == "xpu"):
                client.cancel(job_id)
                cancelled = True
            if status["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        else:
            pytest.fail("XPU scheduler job did not reach a terminal state")
        directory = tmp_path / "artifacts/runtime/scheduler/jobs" / job_id
        if operation == "cancel":
            assert cancelled, "XPU forward progress was never observed"
            assert status["state"] == "cancelled", status
            return
        outcome = json.loads((directory / "outcome.json").read_text(encoding="utf-8"))
        if operation == "stale_uuid":
            assert status["state"] == "failed", status
            assert "UUID" in outcome["error"] and "未回退 CPU" in outcome["error"]
            assert not (directory / "film-performance.json").exists()
            return
        assert status["state"] == "succeeded", status
        performance = json.loads((directory / "film-performance.json").read_text(encoding="utf-8"))
        assert performance["actual_backend"] == "gpu" and performance["gpu_runtime"] == "xpu"
        assert performance["device_uuid"] == device["uuid"]
        assert performance["dtype"] == "float64/complex128"
        assert performance["output_device"].startswith("xpu:")
        execution = outcome["result"]["execution"]
        assert execution["device"] == device and execution["cpu_forward_fallback"] is False
        assert execution["scoring_backend"] == execution["independent_validation_backend"] == "cpu"
        assert outcome["result"]["candidate"]["evaluations"] > 1
        assert outcome["result"]["solver_validation"]["validation_complete"] is True
        # The validator replays both candidate scores and the denser holdout
        # on CPU rather than trusting the execution declaration alone.
        from coat_sim.scheduler.result_capability import validate_film_optimization_result
        assert validate_film_optimization_result(service, outcome["result"]) == outcome["result"]
    finally:
        if client.endpoint.exists():
            client._call("stop", start=False)
            process = client._processes.get(str(client.root))
            if process is not None:
                process.wait(timeout=15)
