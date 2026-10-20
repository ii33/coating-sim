from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal

from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.scheduler.jobs import JobSpec, canonical
from coat_sim.scheduler.remote_server import BlobStore
from coat_sim.scheduler.result_capability import is_synthesis_result
from coat_sim.ui.compute_queue import _annotate_result_availability, _read_queue_result
from coat_sim.ui.project_compute_views import _wire_project_queue_results


def _result(*, state="completed", candidate=True):
    body = {
        "state": state,
        "candidate_design": {"front_layers": []} if candidate else None,
        "validation_complete": candidate,
        "validation_score": {} if candidate else None,
        "training_score": {} if candidate else None,
        "targets_met": candidate and state == "completed",
    }
    audit = {"available": candidate, "targets_met": candidate if state == "completed" else None,
             "continuous_certificate": False, "training_max_normalized_error": 0.0 if candidate else None,
             "audit_max_normalized_error": 0.0 if candidate else None,
             "acceptable_normalized_error": 1.0}
    return {"schema_version": 1, "kind": "coat_sim.film_synthesis_result",
            "request_id": "queue-view-test", "ok": state != "failed", "result": body,
            "review": {"termination": {"state": state, "reason": "test"},
                       "targets_met": body["targets_met"], "layers": [], "per_objective": []},
            "best_candidate_audit": audit, "audit": None,
            "physical_structure_metadata": []}


class _Store:
    def __init__(self, job):
        self.job = job

    def read_job(self, _job_id):
        return deepcopy(self.job)


def test_synthesis_history_result_is_typed_readable_and_available(tmp_path: Path):
    result = _result()
    row = {"job_id": "job-1", "kind": "film_synthesis", "state": "succeeded", "result": result}
    assert is_synthesis_result(result)
    _annotate_result_availability(tmp_path, [row])
    assert row["resultAvailable"] is True
    loaded, asset_path = _read_queue_result(None, _Store(row), "job-1")
    assert loaded == result
    assert loaded is not result
    assert asset_path == ""


def test_synthesis_cancellation_without_candidate_remains_viewable(tmp_path: Path):
    result = _result(state="cancelled", candidate=False)
    row = {"job_id": "job-2", "kind": "film_synthesis", "state": "cancelled", "result": result}
    _annotate_result_availability(tmp_path, [row])
    assert row["resultAvailable"] is True
    loaded, _ = _read_queue_result(None, _Store(row), "job-2")
    assert loaded["result"]["candidate_design"] is None
    assert loaded["best_candidate_audit"]["targets_met"] is None


def test_synthesis_payload_is_not_reinterpreted_under_another_queue_kind():
    result = _result()
    row = {"job_id": "job-wrong-kind", "kind": "film_optimization",
           "state": "succeeded", "result": result}
    _annotate_result_availability(Path("."), [row])
    assert row["resultAvailable"] is False
    with pytest.raises(ValueError, match="does not belong"):
        _read_queue_result(None, _Store(row), row["job_id"])


class _Queue(QObject):
    filmOptimizationResultReady = Signal(str, object)
    filmSynthesisResultReady = Signal(str, object)
    rayTraceResultReady = Signal(str, str)
    cancelReceived = Signal(str, object, str)

    def __init__(self, row):
        super().__init__()
        self.row = row
        self.status = ""

    def _require_owned_job(self, job_id):
        assert job_id == self.row["job_id"]
        return deepcopy(self.row)

    def _result_status(self, message):
        self.status = message


def test_project_queue_routes_exact_typed_payload_to_read_only_synthesis_inspector():
    result = _result()
    origin = {"project_id": "project-a", "project_root": "C:/projects/a"}
    row = {"job_id": "job-3", "kind": "film_synthesis", "result": result, "origin": origin}
    inspected = []

    class Controller:
        project_context = object()

        def inspectSynthesisQueueResult(self, job_id, payload, *, origin=None):
            inspected.append((job_id, deepcopy(payload), origin))
            return True

    controller = Controller()

    class Desktop:
        def open_project_module(self, _workspace, module_key):
            assert module_key == "film_optimization"

        def controller_for(self, _workspace, _module_key):
            return controller

    workspace = cast(ProjectFilmWorkspace, controller.project_context)
    navigation = SimpleNamespace(desktop=Desktop())
    queue = _Queue(row)
    _wire_project_queue_results(queue, workspace, navigation)
    queue.filmSynthesisResultReady.emit("job-3", deepcopy(result))
    assert inspected == [("job-3", result, origin)]
    assert "read-only" in queue.status


def test_project_queue_rejects_payload_that_differs_from_persisted_row():
    result = _result()
    row = {"job_id": "job-4", "kind": "film_synthesis", "result": result,
           "origin": {"project_id": "p", "project_root": "C:/p"}}
    inspected = []
    workspace = cast(ProjectFilmWorkspace, object())

    class Controller:
        project_context = workspace

        def inspectSynthesisQueueResult(self, *_args, **_kwargs):
            inspected.append(True)
            return True

    controller = Controller()
    queue = _Queue(row)
    _wire_project_queue_results(
        queue, workspace,
        SimpleNamespace(desktop=SimpleNamespace(
            open_project_module=lambda *_: None,
            controller_for=lambda *_: controller)))
    altered = deepcopy(result)
    altered["review"]["layers"].append({"index": 0})
    queue.filmSynthesisResultReady.emit("job-4", altered)
    assert inspected == []
    assert "Unable to open" in queue.status


def test_compute_target_offers_synthesis_cpu_only_and_remote_old_host_fails_closed(tmp_path, monkeypatch):
    from coat_sim.ui.compute_target import ComputeTarget, ComputeTargetError, validate_capabilities

    QCoreApplication.instance() or QCoreApplication([])

    class Client:
        def __init__(self, root):
            self.root = root

        def capabilities(self):
            return {"protocol_version": 1, "executors": ["film_synthesis"]}

    monkeypatch.setattr("coat_sim.ui.settings.load_ui_settings", lambda: {})
    target = ComputeTarget(tmp_path, "film_synthesis", client_factory=Client)
    try:
        assert target.canSubmit
        devices = target.property("devices")
        assert devices[0]["id"] == "cpu:default"
        assert devices[0]["backend"] == "CPU"
        target.setTask("film_optimization")
        assert target.supportsFilmXpuProbe is True
        target.setTask("film_synthesis")
        assert target.supportsFilmXpuProbe is False
        assert target.canSubmit
        assert target.deviceId == "cpu:default"
        with pytest.raises(ComputeTargetError) as error:
            validate_capabilities({"protocol_version": 1, "executors": ["film_optimization"]},
                                  "film_synthesis", remote=False)
        assert error.value.code == "unsupported_executor"
        old_remote = {"protocol_version": 1, "api_version": 1,
            "executor_schema_version": 1, "device_schema_version": 1,
            "device_binding_version": 1, "executors": ["film_optimization"],
            "devices": [{"id": "cpu:default", "backend": "CPU", "name": "CPU",
                         "available": True, "executors": ["film_optimization"], "algorithms": []}]}
        with pytest.raises(ComputeTargetError) as old_error:
            validate_capabilities(old_remote, "film_synthesis", remote=True)
        assert old_error.value.code == "unsupported_executor"
        capabilities = {"protocol_version": 1, "api_version": 1,
            "executor_schema_version": 1, "device_schema_version": 1,
            "device_binding_version": 1, "executors": ["film_synthesis"],
            "devices": [{"id": "cpu:default", "backend": "CPU", "name": "CPU",
                         "available": True, "executors": ["film_synthesis"], "algorithms": []}]}
        rows = validate_capabilities(capabilities, "film_synthesis", remote=True)
        assert [row["id"] for row in rows] == ["cpu:default"]
        gpu_only = {**capabilities,
            "devices": [{"id": "cuda:default", "backend": "gpu_cuda", "name": "GPU",
                         "available": True, "physical_id": "gpu-1", "executors": ["film_synthesis"],
                         "algorithms": []}]}
        gpu_rows = target._filter_devices(validate_capabilities(
            gpu_only, "film_synthesis", remote=True))
        assert gpu_rows[0]["available"] is False
        assert "CPU execution only" in gpu_rows[0]["reason"]
    finally:
        target._executor.shutdown(wait=True, cancel_futures=True)


def test_remote_worker_accepts_only_cpu_binding_for_synthesis(tmp_path: Path):
    from coat_sim.scheduler.remote_worker import _check_bound_worker

    job_id = "synthesis-remote"
    directory = tmp_path / "artifacts/runtime/scheduler/jobs" / job_id
    directory.mkdir(parents=True)
    spec = JobSpec("film_synthesis", {"payload": {}},
                   {"device_binding": {"schema_version": 1, "device_id": "cpu:default"}})
    (directory / "spec.json").write_text(canonical(spec.to_dict()), encoding="utf-8")
    assert _check_bound_worker(tmp_path, job_id).executor == "film_synthesis"
    spec = JobSpec("film_synthesis", {"payload": {}},
                   {"device_binding": {"schema_version": 1, "device_id": "cuda:default"}})
    (directory / "spec.json").write_text(canonical(spec.to_dict()), encoding="utf-8")
    with pytest.raises(ValueError, match="remote_film_synthesis_device_mismatch"):
        _check_bound_worker(tmp_path, job_id)


def test_remote_admission_binds_synthesis_to_cpu_and_rejects_gpu(monkeypatch):
    from coat_sim.scheduler import remote_contract

    monkeypatch.setattr(remote_contract, "_validate_film_synthesis_payload", lambda _payload: None)
    cpu = JobSpec("film_synthesis", {"payload": {}}, {
        "device_binding": {"schema_version": 1, "device_id": "cpu:default"}})
    remote_contract._validate_spec(cpu, allow_probe=False, bound=True)
    gpu = JobSpec("film_synthesis", {"payload": {}}, {
        "device_binding": {"schema_version": 1, "device_id": "cuda:default"}})
    with pytest.raises(remote_contract.RemoteContractError) as error:
        remote_contract._validate_spec(gpu, allow_probe=False, bound=True)
    assert error.value.code == "unsupported_device_binding"


def test_remote_service_advertises_synthesis_on_cpu_only(tmp_path: Path):
    from coat_sim.scheduler.remote_server import RemoteComputeService

    service = object.__new__(RemoteComputeService)
    service.appearance_cpu_available = False
    service.appearance_cuda_available = False
    service.appearance_cuda_reason = "not tested"
    service.appearance_cpu_reason = "not tested"
    service.allow_probe = False
    service.token = None
    service.blobs = BlobStore(tmp_path, max_bytes=1024, quota_bytes=2048)
    service.max_connections = 4
    capabilities = RemoteComputeService._capabilities(service)
    assert "film_synthesis" in capabilities["executors"]
    cpu = next(row for row in capabilities["devices"] if row["id"] == "cpu:default")
    gpu = next(row for row in capabilities["devices"] if row["id"] == "cuda:default")
    assert "film_synthesis" in cpu["executors"]
    assert "film_synthesis" not in gpu["executors"]
