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
from coat_sim.ui.compute_queue import (
    _SynthesisQueueResult, _annotate_result_availability, _read_queue_result,
)
from tests.test_film_synthesis_nominal_surfaces import _bind_row
from coat_sim.ui.project_compute_views import _wire_project_queue_results


def _result(*, state="completed", candidate=True):
    # Use a real frozen physical result; an invented old quality flag cannot
    # supply nominal acceptance evidence to the queue capability boundary.
    from dataclasses import replace
    from coat_sim.core.film_auto_synthesis import synthesize_film_design
    from coat_sim.core.film_synthesis_io import film_synthesis_result_to_payload
    from tests.test_film_auto_synthesis import setup_problem
    service, request = setup_problem(upper=.05, low=550., high=550., samples=1)
    result = synthesize_film_design(service, request)
    if state != "completed":
        result = replace(result, state=state, targets_met=False, termination_reason=state)
    if not candidate:
        result = replace(result, candidate_design=None, training_score=None,
                         validation_score=None, validation_complete=False,
                         candidate_provenance={}, solver_acceptable=None)
    return film_synthesis_result_to_payload("queue-view-test", result)


class _Store:
    def __init__(self, root, job):
        self.root = root
        self.job = job

    def read_job(self, _job_id):
        return deepcopy(self.job)


def test_synthesis_history_result_is_typed_readable_and_available(tmp_path: Path):
    result = _result()
    row = {"job_id": "job-1", "kind": "film_synthesis", "state": "succeeded", "result": result}
    _bind_row(tmp_path, row)
    assert is_synthesis_result(result)
    _annotate_result_availability(tmp_path, [row])
    assert row["resultAvailable"] is True
    loaded, asset_path = _read_queue_result(None, _Store(tmp_path, row), "job-1")
    assert isinstance(loaded, _SynthesisQueueResult)
    assert loaded.source_job == row
    assert loaded.payload["result"]["candidate_design"] == result["result"]["candidate_design"]
    assert loaded.payload["result"]["targets_met"] == result["result"]["targets_met"]
    for name in ("training_score", "validation_score"):
        # Replay's evaluator rank key is deliberately not a synthesis search
        # ranking key, but every saved physical condition must be unchanged.
        before = {key: value for key, value in result["result"][name].items() if key != "key"}
        after = {key: value for key, value in loaded.payload["result"][name].items() if key != "key"}
        assert canonical(after) == canonical(before)
    assert loaded.payload is not result
    assert loaded.payload["physical_replay_verification"]["read_only"] is True
    assert asset_path == ""


def test_synthesis_cancellation_without_candidate_remains_viewable(tmp_path: Path):
    result = _result(state="cancelled", candidate=False)
    row = {"job_id": "job-2", "kind": "film_synthesis", "state": "cancelled", "result": result}
    _bind_row(tmp_path, row)
    _annotate_result_availability(tmp_path, [row])
    assert row["resultAvailable"] is True
    loaded, _ = _read_queue_result(None, _Store(tmp_path, row), "job-2")
    assert isinstance(loaded, _SynthesisQueueResult)
    loaded = loaded.payload
    assert loaded["result"]["candidate_design"] is None
    assert loaded["best_candidate_audit"]["targets_met"] is None


def test_synthesis_payload_is_not_reinterpreted_under_another_queue_kind():
    result = _result()
    row = {"job_id": "job-wrong-kind", "kind": "film_optimization",
           "state": "succeeded", "result": result}
    _annotate_result_availability(Path("."), [row])
    assert row["resultAvailable"] is False
    with pytest.raises(ValueError, match="does not belong"):
        _read_queue_result(None, _Store(Path("."), row), row["job_id"])


class _Queue(QObject):
    filmOptimizationResultReady = Signal(str, object)
    filmSynthesisResultReady = Signal(str, object)
    rayTraceResultReady = Signal(str, str)
    cancelReceived = Signal(str, object, str)

    def __init__(self, row, store=None, loaded=None):
        super().__init__()
        self.row = row
        self.store = store
        self._result_viewer = (None if loaded is None else SimpleNamespace(
            _shown_job=row["job_id"], _shown_kind="film_synthesis",
            _shown_synthesis_source_job=deepcopy(loaded.source_job),
            _shown_film_result=deepcopy(loaded.payload)))
        self.status = ""

    def _require_owned_job(self, job_id):
        assert job_id == self.row["job_id"]
        return deepcopy(self.row)

    def _result_status(self, message):
        self.status = message


def test_project_queue_routes_exact_typed_payload_to_read_only_synthesis_inspector(tmp_path):
    result = _result()
    origin = {"project_id": "project-a", "project_root": "C:/projects/a"}
    row = {"job_id": "job-3", "kind": "film_synthesis", "state": "succeeded",
           "result": result, "origin": origin}
    _bind_row(tmp_path, row)
    store = _Store(tmp_path, row)
    loaded, _ = _read_queue_result(None, store, row["job_id"])
    assert isinstance(loaded, _SynthesisQueueResult)
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
    queue = _Queue(row, store)
    _wire_project_queue_results(queue, workspace, navigation)
    queue.filmSynthesisResultReady.emit("job-3", deepcopy(result))
    assert inspected == []
    assert "Unable to open" in queue.status
    queue._result_viewer = SimpleNamespace(_shown_job=row["job_id"],
        _shown_kind="film_synthesis", _shown_synthesis_source_job=deepcopy(loaded.source_job),
        _shown_film_result=deepcopy(loaded.payload))
    queue.filmSynthesisResultReady.emit("job-3", deepcopy(loaded.payload))
    assert inspected == [("job-3", loaded.payload, origin)]
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
