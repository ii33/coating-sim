"""GPU forward selection stays distinct from the outer compute host route."""
from __future__ import annotations

from copy import deepcopy
import json
import os
import sys
import threading

import pytest

from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
from coat_sim.core.film_synthesis_forward import validate_forward_device
from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.ui.film_synthesis_forward_probe import FilmSynthesisForwardProbe
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer, window_runtime

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.instance() or QGuiApplication([])


def _select_material(controller):
    for row in controller.structureMaterialOptions:
        if row["material_id"] == "acceptance-film" and row["optical_set_id"] == "film-set":
            assert controller.setStructureMaterialSelected(row["material_id"], row["optical_set_id"], True)
            return
    raise AssertionError("synthetic film source is missing")


def _binding(backend="xpu", index=0):
    return {"runtime": backend, "index": index, "uuid": None,
            "name": f"Synthetic {backend.upper()}", "torch_version": "2.test",
            "dtype": "float64/complex128"}


def test_forward_device_schema_is_pure_and_exact():
    assert validate_forward_device(None, "cpu") is None
    binding = _binding()
    assert validate_forward_device(binding, "xpu") == binding
    with pytest.raises(ValueError, match="CPU forward"):
        validate_forward_device(binding, "cpu")
    with pytest.raises(ValueError, match="runtime/dtype"):
        validate_forward_device({**binding, "runtime": "cuda"}, "xpu")
    with pytest.raises(ValueError, match="invalid identity fields"):
        validate_forward_device({**binding, "unexpected": True}, "xpu")


def test_probe_runs_isolated_marker_commands_and_reports_devices(app, tmp_path):
    binding = _binding()

    def command(backend):
        payload = ({"ok": True, "backend": backend, "devices": [binding], "error": None}
                   if backend == "xpu" else
                   {"ok": False, "backend": backend, "devices": [], "error": "runtime unavailable"})
        code = "import json,sys;print('__COAT_FILM_SYNTHESIS_FORWARD__='+json.dumps(" + repr(payload) + "))"
        return [sys.executable, "-c", code]

    probe = FilmSynthesisForwardProbe(cwd=tmp_path, command_factory=command)
    completed = []
    probe.finished.connect(lambda generation, rows: completed.append((generation, rows)))
    try:
        probe.probe()
        for _ in range(300):
            app.processEvents()
            if completed:
                break
            from PySide6.QtTest import QTest
            QTest.qWait(10)
        assert completed
        _, rows = completed[0]
        assert rows["xpu"]["available"] is True
        assert rows["xpu"]["devices"] == [binding]
        assert rows["cuda"]["available"] is False
        assert "runtime unavailable" in rows["cuda"]["reason"]
        assert "XPU" in probe.status
    finally:
        probe.shutdown()


def test_probe_cancel_reaps_nested_child_and_fences_completion(app, tmp_path):
    pid_file = tmp_path / "probe-pids.json"
    child = "import time;time.sleep(30)"
    code = ("import json,os,subprocess,sys,time;"
            f"p=subprocess.Popen([sys.executable,'-c',{child!r}]);"
            f"open({str(pid_file)!r},'w',encoding='utf-8').write(json.dumps([os.getpid(),p.pid]));"
            "time.sleep(30)")
    probe = FilmSynthesisForwardProbe(
        cwd=tmp_path, command_factory=lambda _backend: [sys.executable, "-c", code])
    completed = []
    probe.finished.connect(lambda *args: completed.append(args))
    probe.probe()
    from PySide6.QtTest import QTest
    for _ in range(200):
        app.processEvents()
        if pid_file.exists():
            break
        QTest.qWait(10)
    assert pid_file.exists()
    pids = json.loads(pid_file.read_text(encoding="utf-8"))
    probe.cancel()
    assert not probe.busy
    for _ in range(100):
        app.processEvents()
        if probe._active_process is None:
            break
        QTest.qWait(10)
    assert completed == []
    assert probe._active_process is None
    for pid in pids:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                exit_code = wintypes.DWORD()
                kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                kernel.CloseHandle(handle)
                assert exit_code.value != 259
        else:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    probe.shutdown()


def test_controller_freezes_selected_forward_backend_and_legacy_config_defaults_cpu(
        app, tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        monkeypatch.setattr(controller._synthesis_forward_probe, "probe", lambda: None)
        assert controller.setSearchMode("synthesis")
        _select_material(controller)
        assert controller.setObjectiveField("wavelength_min_nm", 500.0)
        controller._synthesis_forward_probe._results["xpu"] = {
            "backend": "xpu", "available": True, "devices": [_binding()], "reason": ""}
        assert controller.setSynthesisForwardBackend("xpu")
        request = freeze_film_synthesis_request(service, controller._build_synthesis_request())
        assert request.forward_backend == "xpu"
        assert request.forward_device == _binding()
        payload = film_synthesis_request_to_payload("ui-xpu-selection", request)
        assert payload["request"]["forward_backend"] == "xpu"
        assert payload["request"]["forward_device"] == _binding()

        saved = controller.exportOptimizationConfiguration()
        assert saved["synthesis"]["forward_backend"] == "xpu"
        assert saved["synthesis"]["forward_device"] == _binding()
        assert controller.setSynthesisForwardBackend("cpu")
        assert controller.loadOptimizationConfiguration(saved)
        assert controller.synthesisForwardBackend == "xpu"
        assert controller.synthesisForwardDevice == _binding()

        legacy = deepcopy(saved)
        legacy["synthesis"].pop("forward_backend")
        legacy["synthesis"].pop("forward_device")
        assert controller.loadOptimizationConfiguration(legacy)
        assert controller.synthesisForwardBackend == "cpu"
        assert controller.synthesisForwardDevice == {}


def test_synthesis_window_exposes_forward_probe_separately_from_host_selector(app, tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        monkeypatch.setattr(controller._synthesis_forward_probe, "probe", lambda: None)
        assert controller.setSearchMode("synthesis")
        with window_runtime(app, controller) as (window, warnings):
            from PySide6.QtCore import QObject

            assert window.findChild(QObject, "filmSynthesisForwardBackendBox") is not None
            assert window.findChild(QObject, "filmSynthesisForwardDeviceBox") is not None
            assert window.findChild(QObject, "filmSynthesisForwardProbeButton") is not None
            assert controller.computeTarget.task == "film_synthesis"
            assert controller.computeTarget.hostId == "local"
            assert not any("ReferenceError" in row or "TypeError" in row for row in warnings), warnings


def test_ui_cpu_two_worker_setting_reaches_real_typed_job_and_history(app, tmp_path, monkeypatch):
    """Exercise actual process-pool work from a controller-built typed request."""
    from coat_sim.core.film_design_service import FilmDesignService
    from coat_sim.core.film_synthesis_jobs import (
        build_job_payload, canonical_parameters, deserialize_job_payload, execute_job,
    )
    from coat_sim.core.material_business import MaterialLibrary
    from coat_sim.core.materials import default_material_profile
    from tools.run_film_synthesis_acceptance import atomic_json, synthetic_service

    _unused_service, _environment, optical_rows = synthetic_service()
    match_row = deepcopy(next(row for row in optical_rows if row["id"] == "match"))
    for index in range(1, 4):
        extra = deepcopy(match_row)
        extra.update(id=f"extra-{index}", name=f"synthetic extra {index}")
        optical_rows.append(extra)
    library = MaterialLibrary(tmp_path)
    for source in optical_rows:
        category = "substrate" if source["id"] == "glass" else "film"
        record = default_material_profile("GUI worker fixture " + source["id"], id=source["id"])
        record.update(category=category, optical_constants=deepcopy(source["optical_sets"]))
        library.write(record)
    service = FilmDesignService(tmp_path)
    design = service.new_design()
    design["substrate"].update(material_id="glass", optical_set_id="nk")
    design["front_layers"] = []

    with optimizer(service, design) as (controller, _source, _session):
        monkeypatch.setattr(controller._synthesis_forward_probe, "probe", lambda: None)
        selected_pairs = {(identifier, "nk") for identifier in
                          ("match", "low", "high", "extra-1", "extra-2", "extra-3")}
        for row in controller.structureMaterialOptions:
            pair = (row["material_id"], row["optical_set_id"])
            if pair in selected_pairs:
                assert controller.setStructureMaterialSelected(pair[0], pair[1], True)
                selected_pairs.remove(pair)
        assert not selected_pairs
        assert controller.setSearchMode("synthesis")
        assert controller.setObjectiveField("wavelength_min_nm", 450)
        assert controller.setObjectiveField("wavelength_max_nm", 900)
        assert controller.setObjectiveField("wavelength_samples", 4001)
        assert controller.setObjectiveField("angle_max_deg", 60)
        assert controller.setObjectiveField("angle_step_deg", 30)
        assert controller.setObjectiveField("upper_bound_percent", 100)
        assert controller.setObjectiveField("lower_bound_percent", 99.9999)
        assert controller.setSynthesisCpuWorkers(2)
        assert controller.setSynthesisOption("max_cycles", 1)
        assert controller._synthesis_forward_backend == "cpu"
        assert not controller._active_configuration_error()

        unbounded_request = freeze_film_synthesis_request(service, controller._build_synthesis_request())
        assert unbounded_request.max_evaluations is None
        # This checks real worker routing, accounting and history, not optical
        # convergence. Keep its full physical request but explicitly fund one
        # complete four-candidate process batch, plus baseline and audit work.
        assert controller.setSynthesisOption("max_evaluations", 8)
        request = freeze_film_synthesis_request(service, controller._build_synthesis_request())
        assert request.cpu_workers == 2
        assert request.objectives[0].wavelength_samples == 4001
        assert len(request.objectives[0].angles_deg) == 3
        assert len(request.candidate_materials) == 6
        assert request.max_evaluations == 8
        from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
        request_payload = film_synthesis_request_to_payload("ui-two-worker-real", request)
        parameters = canonical_parameters(None, {"payload": request_payload})
        job = build_job_payload(parameters)
        _, queued_request, _ = deserialize_job_payload(job)
        assert queued_request.cpu_workers == 2
        assert queued_request.max_evaluations == 8

        reports = []
        result = execute_job({"payload": job}, threading.Event(),
                             lambda value, **_kwargs: reports.append(value),
                             tmp_path / "job.json", atomic_json, str(tmp_path))
        performance = result["result"]["performance"]
        assert performance["backend"] == "headless_process", {
            key: performance.get(key) for key in ("fallback_events", "parallel_batches",
                "submitted_tasks", "completed_tasks", "effective_workers", "error_tasks",
                "training_evaluator_invocations", "training_physical_evaluations")}
        assert performance["effective_workers"] == 2
        assert len(performance["worker_pids"]) == 2
        assert performance["parallel_batches"] > 0
        assert performance["submitted_tasks"] > 0
        assert any(row["phase"] == "structure" and row["parallel_selected"]
                   and row["budget_allows"] and row["candidate_count"] == 4
                   for row in performance["batch_decisions"])
        assert result["result"]["evaluations"] <= 8
        assert performance["cleanup_complete"] is True
        assert performance["submitted_tasks"] == performance["completed_tasks"]
        assert performance["completed_tasks"] == performance["consumed_tasks"]
        assert performance["unknown_tasks"] == 0
        print("two-worker budget evidence:", json.dumps({
            "evaluation_budget": request.max_evaluations,
            "logical_evaluations": result["result"]["evaluations"],
            "parallel_batches": performance["parallel_batches"],
            "worker_pids": performance["worker_pids"],
            "submitted_tasks": performance["submitted_tasks"],
            "completed_tasks": performance["completed_tasks"],
            "consumed_tasks": performance["consumed_tasks"],
            "unknown_tasks": performance["unknown_tasks"],
            "cleanup_complete": performance["cleanup_complete"],
        }, sort_keys=True))

        controller._synthesis_payload = result
        summary = controller.synthesisExecutionSummary
        assert "CPU 多进程" in summary
        assert "实际工作进程数：2" in summary



def _remote_synthesis_target(monkeypatch, controller):
    from types import SimpleNamespace

    class RemoteStore:
        def __init__(self):
            self.transport = SimpleNamespace(remote_http=True, device_id="remote-device")
            self.submissions = []

        def capabilities(self):
            return {
                "protocol_version": 1,
                "api_version": 1,
                "executor_schema_version": 1,
                "device_schema_version": 1,
                "device_binding_version": 1,
                "executors": ["film_synthesis"],
                "devices": [{
                    "id": "remote-device",
                    "backend": "CPU",
                    "available": True,
                    "executors": ["film_synthesis"],
                    "algorithms": [],
                }],
            }

        def submit(self, spec, *, job_id, metadata):
            self.submissions.append((spec, job_id, metadata))

    store = RemoteStore()
    target = controller.computeTarget
    monkeypatch.setattr(target, "client_for_submit",
                        lambda **_kwargs: (store, {"id": "remote-device", "backend": "CPU"}))
    monkeypatch.setattr(target, "admission_verifier",
                        lambda *_args: (lambda: None, lambda: True))
    return store


def _set_remote_guard_objective(controller):
    for field, value in (
        ("wavelength_min_nm", 450.0),
        ("wavelength_max_nm", 700.0),
        ("wavelength_samples", 3),
        ("lower_bound_percent", 0.0),
        ("upper_bound_percent", 100.0),
    ):
        assert controller.setObjectiveField(field, value)


def test_remote_synthesis_rejects_local_gpu_forward_before_submit(app, tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        monkeypatch.setattr(controller._synthesis_forward_probe, "probe", lambda: None)
        assert controller.setSearchMode("synthesis")
        _select_material(controller)
        _set_remote_guard_objective(controller)
        controller._synthesis_forward_probe._results["xpu"] = {
            "backend": "xpu", "available": True, "devices": [_binding()], "reason": ""}
        assert controller.setSynthesisForwardBackend("xpu")
        store = _remote_synthesis_target(monkeypatch, controller)

        assert not controller._start_optimization()
        assert not store.submissions
        assert not controller.busy
        assert "本机" in controller.error


def test_prepared_angle_remote_synthesis_rejects_local_gpu_forward(app, tmp_path, monkeypatch):
    from dataclasses import replace
    from coat_sim.ui.film_optimization_quick_controller import _PreparedAngleGrid

    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        monkeypatch.setattr(controller._synthesis_forward_probe, "probe", lambda: None)
        assert controller.setSearchMode("synthesis")
        _select_material(controller)
        _set_remote_guard_objective(controller)
        cpu_request = controller._build_synthesis_request()
        gpu_request = replace(cpu_request, forward_backend="xpu", forward_device=_binding())
        store = _remote_synthesis_target(monkeypatch, controller)

        assert not controller._start_optimization(
            prepared_start=_PreparedAngleGrid(gpu_request, {}))
        assert not store.submissions
        assert not controller.busy
        assert "本机" in controller.error


def test_cpu_forward_remote_synthesis_keeps_existing_admission_path(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QThreadPool
    from coat_sim.ui import film_optimization_run_coordinator as coordinator_module

    service, _library, design = fixture(tmp_path)

    class HeldPool:
        def start(self, worker):
            self.worker = worker

    pool = HeldPool()
    monkeypatch.setattr(QThreadPool, "globalInstance", lambda: pool)
    with optimizer(service, design) as (controller, _source, _session):
        monkeypatch.setattr(controller._synthesis_forward_probe, "probe", lambda: None)
        assert controller.setSearchMode("synthesis")
        _select_material(controller)
        _set_remote_guard_objective(controller)
        store = _remote_synthesis_target(monkeypatch, controller)

        assert controller._start_optimization(), controller.error
        worker = pool.worker
        assert worker.remote_admission is not None
        assert worker.request.design_snapshot["front_layers"] == []
        worker.remote_admission()
        assert len(store.submissions) == 1
        submitted_spec = store.submissions[0][0]
        assert submitted_spec["executor"] == "film_synthesis"
        assert submitted_spec["parameters"]["payload"]["request"]["request"]["forward_backend"] == "cpu"

        coordinator = controller._run_coordinator
        operation_id = worker.operation_id
        coordinator._on_worker_finished(
            operation_id, None, [], {"state": "cancelled", "message": "test cleanup"})
        coordinator_module._release_remote_worker(worker)
        assert not controller.busy


@pytest.mark.parametrize(
    ("actual_backend", "expected_label"),
    [("cuda", "CUDA"), ("xpu", "XPU"), ("rocm", "ROCm"), (None, "未记录")],
)
def test_history_summary_labels_actual_forward_backend(app, tmp_path, monkeypatch,
                                                        actual_backend, expected_label):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        monkeypatch.setattr(controller._synthesis_forward_probe, "probe", lambda: None)
        controller._synthesis_payload = {"result": {"performance": {
            "forward": {"requested_backend": "xpu", "actual_backend": actual_backend}}}}
        assert expected_label in controller.synthesisExecutionSummary
