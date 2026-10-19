"""Desktop selection, queue compatibility and opt-in real Intel execution."""

from concurrent.futures import Future
from copy import deepcopy
import json
import os
from pathlib import Path
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


def test_cpu_default_explicit_batch_identity_confirmation_and_context_changes(app, tmp_path):
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
        deliver(target)
        assert target.deviceId == "cpu:default"
        target.setDeviceId("xpu:0")
        assert target.canSubmit and controller._build_request().execution_device == BINDING
        controller.setObjectiveField("angle_max_deg", 20.0)
        assert target.deviceId == "xpu:0" and not target.canSubmit
        assert not controller.startOptimization()
        assert "固定角度" in controller.error
        target.setDeviceId("cpu:default")
        assert target.canSubmit


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
            assert window.findChild(QObject, "computeFilmXpuProbeButton") is not None
            assert not warnings


@pytest.mark.real_gpu
@pytest.mark.skipif(
    os.environ.get("COAT_SIM_TEST_XPU") != "1", reason="explicit native Intel XPU run only"
)
@pytest.mark.parametrize("operation", ["complete", "cancel", "edited", "stale_uuid"])
def test_native_ui_to_queue_xpu_result_and_lifecycle(app, tmp_path, operation):
    service, _library, design = fixture(tmp_path)
    client = SchedulerClient(tmp_path)
    assert not client.endpoint.exists(), "test must own a fresh queue root"
    try:
        with optimizer(service, design) as (controller, source, session):
            controller.setSolver("differential_evolution")
            controller.setDeferredBatchEnabled(True)
            controller.setObjectiveField("wavelength_samples", 101)
            controller.setObjectiveField("mode", "value")
            controller.setObjectiveField("target_value_percent", 0.0)
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
            target.requestFilmXpuProbe()  # Actual bounded native child, actual private queue capabilities.
            spin(app, lambda: not target.filmXpuProbeBusy, 40)
            actual = next(
                row for row in cast(list[dict[str, Any]], target.devices) if row["backend"] == "xpu"
            )
            assert actual["available"], actual.get("reason", actual)
            assert target.deviceId == "cpu:default"
            if operation == "stale_uuid":
                changed = "00000000-0000-0000-0000-000000000002"
                for row in target._all_devices:
                    if row["backend"] == "xpu":
                        row["physical_id"] = changed
                        row["execution_device"]["uuid"] = changed
                target._devices = target._filter_devices(target._all_devices)
            target.setDeviceId(actual["id"])
            assert target.canSubmit
            progress_text = []
            controller.changed.connect(lambda: progress_text.append(controller.progressText))
            assert controller.startOptimization(), controller.error
            job_id = controller._queue_job_id
            assert (
                job_id
                and controller._frozen_request.execution_device["uuid"] == actual["physical_id"]
            )
            if operation == "cancel":
                spin(
                    app,
                    lambda: client.status(job_id).get("progress", {}).get("gpu_runtime") == "xpu",
                    60,
                )
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
            if operation == "stale_uuid":
                assert status["state"] == "failed", status
                outcome = json.loads((directory / "outcome.json").read_text(encoding="utf-8"))
                assert "UUID" in outcome["error"] and "未回退 CPU" in outcome["error"]
                assert not source.applied and not controller.canApply
                assert not (directory / "film-performance.json").exists()
                return
            assert status["state"] == "succeeded", status
            outcome = json.loads((directory / "outcome.json").read_text(encoding="utf-8"))
            assert not controller.error if operation == "complete" else "编辑" in controller.error
            performance = json.loads(
                (directory / "film-performance.json").read_text(encoding="utf-8")
            )
            assert performance["actual_backend"] == "gpu" and performance["gpu_runtime"] == "xpu"
            assert performance["device_uuid"] == actual["physical_id"]
            assert performance["dtype"] == "float64/complex128" and performance[
                "output_device"
            ].startswith("xpu:")
            assert outcome["result"]["execution"]["cpu_forward_fallback"] is False
            assert outcome["result"]["candidate"]["evaluations"] > 1
            assert any("Intel XPU 批量前向" in text for text in progress_text)
            assert bool(source.applied) == (operation == "complete")
            assert controller._result.validation_complete
            evidence = os.environ.get("COAT_SIM_XPU_EVIDENCE")
            if evidence and operation == "complete":
                destination = Path(evidence)
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "ui-queue.json").write_text(
                    json.dumps(
                        {
                            "root": str(tmp_path),
                            "performance": performance,
                            "execution": outcome["result"]["execution"],
                            "applied": bool(source.applied),
                            "progress": progress_text,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                with window_runtime(app, controller) as (window, warnings):
                    assert not warnings
                    assert window.grabWindow().save(str(destination / "native-xpu-result.png"))
    finally:
        if client.endpoint.exists():
            client._call("stop", start=False)  # Only this test's unique, previously absent queue.
            process = client._processes.get(str(client.root))
            if process is not None:
                process.wait(timeout=15)
