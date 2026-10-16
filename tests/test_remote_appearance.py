"""Offline contracts for portable remote appearance input staging."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import importlib.util
import json
import subprocess
import threading
from pathlib import Path

import pytest

from coat_sim.scheduler.jobs import JobSpec
from coat_sim.scheduler.remote_server import BlobStore
from coat_sim.scheduler.store import SchedulerStore


class _Scheduler:
    def __init__(self, root: Path):
        self.store = SchedulerStore(root)
        self.condition = threading.RLock()
        self.stopping = threading.Event()
        self.artifact_index = {}


class _Service:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.blobs = BlobStore(root, max_bytes=2 * 1024 * 1024, quota_bytes=2 * 1024 * 1024)
        self.scheduler = _Scheduler(root)

    def _artifact(self, artifact_id):
        item = self.scheduler.artifact_index.get(artifact_id)
        if item is None:
            raise ValueError("artifact_not_found")
        return item, Path(item["local_path"])


def _stage_asset(service: _Service, root: Path) -> dict:
    """Make a tiny genuine v7 asset and stage its bytes as an uploaded blob."""
    from tests.test_appearance_compute_entrypoints import _write_synthetic_v7_asset

    source = _write_synthetic_v7_asset(root)
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    service.blobs.receive(digest, len(payload), io.BytesIO(payload))

    from coat_sim.scheduler.remote_appearance import register_appearance_bundle

    return register_appearance_bundle(service, digest, len(payload))


def _portable_spec(descriptor: dict, *, display_exposure: float = 0.01) -> JobSpec:
    from tests.test_appearance_compute_entrypoints import _render_inputs

    request = {
        "render_inputs": _render_inputs(),
        "geometry_thickness_mode": "explicit_override",
        "geometry_thickness_mm": 1.0,
        "display_exposure": display_exposure,
        "backend": "cpu",
        "precision": "native",
    }
    return JobSpec(
        "appearance_render",
        {"request": request},
        {
            "appearance_artifact": {
                "artifact_id": descriptor["artifact_id"],
                "sha256": descriptor["sha256"],
            },
            "device_binding": {"schema_version": 1, "device_id": "cpu:default"},
        },
    )


pytestmark = pytest.mark.contract


def test_binary_probe_json_ignores_native_stderr_encoding_but_rejects_bad_stdout():
    from coat_sim.scheduler.remote_appearance import _probe_json_stdout

    good = subprocess.CompletedProcess([], 0, b"native banner\n{\"available\":true}\n",
                                       b"native diagnostic \xce\xff")
    assert _probe_json_stdout(good) == {"available": True}
    with pytest.raises(ValueError):
        _probe_json_stdout(subprocess.CompletedProcess([], 1, good.stdout, good.stderr))
    with pytest.raises(UnicodeError):
        _probe_json_stdout(subprocess.CompletedProcess([], 0, b"{\"bad\":\"\xce\"}", b""))


def test_gpu_acceptance_preflight_preserves_safe_binary_diagnostic(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "tools" / "remote_gpu_acceptance.py"
    spec = importlib.util.spec_from_file_location("gpu_acceptance_binary_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen = {}

    def completed(*_args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess([], 7, b"banner\n", b"native \xce\xff")

    monkeypatch.setattr(module.subprocess, "run", completed)
    with pytest.raises(module.RuntimeFactsError) as raised:
        module._runtime_facts(tmp_path / "python", tmp_path, {"PATH": "test"})
    error = raised.value
    assert error.code == "appearance_runtime_import_failed"
    assert error.diagnostic["child_returncode"] == 7
    assert error.diagnostic["stderr_bytes"] == len(b"native \xce\xff")
    assert "\xce" not in json.dumps(error.diagnostic)
    assert seen["env"]["PYTHONIOENCODING"] == "utf-8"
    private = tmp_path / "private-preflight"
    module._write_private_preflight_bytes(private, tmp_path / "source", error)
    assert (private / "runtime-stderr.bin").read_bytes() == b"native \xce\xff"


@pytest.mark.parametrize("systemroot_key", ["SYSTEMROOT", "SystemRoot", "systemroot"])
def test_gpu_acceptance_keeps_windows_os_env_case_insensitively(
        tmp_path, monkeypatch, systemroot_key):
    script = Path(__file__).resolve().parents[1] / "tools" / "remote_gpu_acceptance.py"
    spec = importlib.util.spec_from_file_location("gpu_acceptance_env_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    supplied = {
        systemroot_key: r"C:\Windows", "Path": r"C:\Windows\System32",
        "CoMsPeC": r"C:\Windows\System32\cmd.exe",
        "CUDA_VISIBLE_DEVICES": "0", "NVIDIA_VISIBLE_DEVICES": "all",
        "COAT_TEST_SECRET": "must-be-removed", "REMOTE_BEARER_TOKEN": "must-be-removed",
    }
    monkeypatch.setattr(module.os, "environ", supplied)
    module._safe_process_env(tmp_path)
    assert supplied[systemroot_key] == r"C:\Windows"
    assert supplied["Path"] == r"C:\Windows\System32"
    assert supplied["CoMsPeC"] == r"C:\Windows\System32\cmd.exe"
    assert supplied["CUDA_VISIBLE_DEVICES"] == "0"
    assert supplied["NVIDIA_VISIBLE_DEVICES"] == "all"
    assert "COAT_TEST_SECRET" not in supplied
    assert "REMOTE_BEARER_TOKEN" not in supplied


def test_register_appearance_bundle_verifies_digest_and_publishes_confined_path(tmp_path):
    from coat_sim.scheduler.remote_appearance import register_appearance_bundle

    service = _Service(tmp_path)
    descriptor = _stage_asset(service, tmp_path)

    expected_root = tmp_path.resolve() / "data" / "remote-appearance-assets"
    staged = Path(descriptor["local_path"])
    assert descriptor["role"] == "appearance_bundle"
    assert descriptor["name"] == "input.coatasset"
    assert descriptor["sha256"] == hashlib.sha256(staged.read_bytes()).hexdigest()
    assert descriptor["bytes"] == staged.stat().st_size
    assert staged == expected_root / descriptor["sha256"] / "input.coatasset"
    assert staged.is_relative_to(tmp_path.resolve())
    assert not staged.is_relative_to(service.blobs.directory)

    # Re-registering identical content keeps the same deterministic identity.
    assert register_appearance_bundle(service, descriptor["sha256"], descriptor["bytes"]) == descriptor


def test_prepare_appearance_submission_replays_same_id_and_rejects_conflict(tmp_path):
    from coat_sim.scheduler.remote_appearance import prepare_appearance_submission

    service = _Service(tmp_path)
    descriptor = _stage_asset(service, tmp_path)

    # This boundary test does not execute the CPU renderer or rebuild the large
    # frozen layer snapshot; the production preflight still checks the asset and
    # canonical request before this freeze adapter is reached.
    def fake_freeze(_service, supplied):
        return {"frozen": {"request": deepcopy(supplied["request"])}}

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("coat_sim.core.appearance_compute.canonical_appearance_parameters", fake_freeze)
        patch.setattr("coat_sim.core.appearance_compute.validate_frozen", lambda _service, envelope: envelope)
        portable = _portable_spec(descriptor)
        prepared = prepare_appearance_submission(service, portable, "same-job")
        service.scheduler.store.submit(prepared, job_id="same-job")

        replay = prepare_appearance_submission(service, portable, "same-job")
        assert replay == prepared

        conflict = _portable_spec(descriptor, display_exposure=0.02)
        with pytest.raises(ValueError, match="request_id_conflict"):
            prepare_appearance_submission(service, conflict, "same-job")


def test_prepare_appearance_submission_rejects_missing_input_artifact(tmp_path):
    from coat_sim.scheduler.remote_appearance import prepare_appearance_submission

    service = _Service(tmp_path)
    descriptor = {
        "artifact_id": "a" * 64,
        "sha256": "b" * 64,
    }
    with pytest.raises(ValueError, match="artifact_not_found"):
        prepare_appearance_submission(service, _portable_spec(descriptor), "missing-asset-job")


def test_materialize_appearance_result_rejects_incomplete_artifact_set_without_writing(tmp_path):
    from coat_sim.scheduler.remote_appearance import materialize_appearance_result

    class Client:
        root = tmp_path

        def status(self, job_id):
            assert job_id == "render-job"
            return {"state": "succeeded", "input_digest": "c" * 64}

        def materialize_artifact(self, *_args):
            pytest.fail("an incomplete result must be rejected before download")

    destination = tmp_path / "data" / "remote-appearance" / "render-job"
    result = {
        "kind": "appearance_render",
        "input_digest": "c" * 64,
        "result_artifacts": [],
    }
    with pytest.raises(ValueError, match="appearance_remote_artifacts_incomplete"):
        materialize_appearance_result(Client(), "render-job", result, destination)
    assert not destination.exists()


def test_real_loopback_cpu_appearance_upload_queue_and_six_sha_downloads(tmp_path):
    """Execute the real scalar renderer across the authenticated HTTP boundary."""
    import json
    from coat_sim.scheduler.remote_appearance import (
        materialize_appearance_result, probe_appearance_cpu_runtime,
    )
    from coat_sim.scheduler.remote_client import make_remote_client
    from tests.test_appearance_compute_entrypoints import _write_synthetic_v7_asset
    from tests.test_remote_server import running

    if not probe_appearance_cpu_runtime(tmp_path)[0]:
        pytest.skip("real Mitsuba scalar CPU runtime is not installed")
    client_root = tmp_path / "client"
    client_root.mkdir()
    source = _write_synthetic_v7_asset(client_root)
    with running(tmp_path / "server") as service:
        client = make_remote_client(
            service.url, client_root, token="ephemeral-test-operator-token",
            allow_loopback_http=True, device_id="cpu:default",
        )
        devices = client.transport.capabilities()["devices"]
        assert any(row["id"] == "cpu:default" and row["available"]
                   and "appearance_render" in row["executors"] for row in devices)
        artifact = client.transport.register_appearance_asset(source)
        assert artifact["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        spec = _portable_spec(artifact)
        receipt = client.submit(spec.to_dict(), job_id="remote-appearance-real-cpu")
        assert receipt["requested_device_id"] == "cpu:default"
        status = client.wait_for_terminal("remote-appearance-real-cpu", wait_s=0.1)
        assert status["state"] == "succeeded", status
        assert status["executed_device_id"] == "cpu:default"
        result = client.result("remote-appearance-real-cpu")
        assert result["backend"] == "cpu" and result["precision"] == "native"
        assert len(result["result_artifacts"]) == 6
        destination = client_root / "data" / "remote-appearance" / "remote-appearance-real-cpu"
        local = materialize_appearance_result(client, "remote-appearance-real-cpu",
                                              result, destination)
        assert Path(local["output_dir"]) == destination
        assert hashlib.sha256(Path(local["index_path"]).read_bytes()).hexdigest() == result["sha256"]
        assert {path.name for path in destination.iterdir()} == {
            "appearance-result.json", "display.png", "manifest.json",
            "radiance_spectrum.npy", "xyz.npy", "linear_srgb.npy",
        }
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["execution"]["backend"] == "cpu_scalar"
        for descriptor in result["result_artifacts"]:
            file = destination / descriptor["name"]
            assert file.stat().st_size == descriptor["bytes"]
            assert hashlib.sha256(file.read_bytes()).hexdigest() == descriptor["sha256"]


def test_bound_cuda_result_has_seven_atomic_sha_verified_artifacts(tmp_path):
    """Offline GPU contract data, without claiming a CUDA execution on CI."""
    import json
    from coat_sim.scheduler.jobs import canonical
    from coat_sim.scheduler.remote_appearance import (
        attach_appearance_output_artifacts, materialize_appearance_result,
    )
    from tests.test_appearance_gpu_queue_contract import AppearanceGpuQueueContractTests

    service_root = tmp_path / "server"
    service_root.mkdir()
    job_id, result, render_dir = AppearanceGpuQueueContractTests()._gpu_fixture(service_root)
    spec_path = service_root / "artifacts/runtime/scheduler/jobs" / job_id / "spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["inputs"] = {"device_binding": {"schema_version": 1,
                                         "device_id": "cuda:default"}}
    spec_path.write_text(canonical(spec), encoding="utf-8")
    result["input_digest"] = hashlib.sha256(canonical(spec).encode()).hexdigest()
    index_path = spec_path.with_name("appearance-result.json")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["input_digest"] = result["input_digest"]
    index_path.write_text(canonical(index), encoding="utf-8")
    from coat_sim.core.appearance_result import verified_output_references
    result["outputs"] = verified_output_references(service_root, job_id, index)
    result["sha256"] = hashlib.sha256(index_path.read_bytes()).hexdigest()
    result["artifact_id"] = hashlib.sha256(
        (job_id + ":result:" + result["sha256"]).encode()).hexdigest()
    result["result_artifacts"] = [{
        "artifact_id": result["artifact_id"], "role": "result",
        "sha256": result["sha256"], "bytes": index_path.stat().st_size,
        "name": "appearance-result.json", "local_path": str(index_path),
    }]
    published = attach_appearance_output_artifacts(service_root, job_id, result)
    assert len(published["result_artifacts"]) == 7
    assert {item["role"] for item in published["result_artifacts"]} == {
        "result", "appearance_display", "appearance_manifest",
        "appearance_radiance_spectrum", "appearance_xyz",
        "appearance_linear_srgb", "appearance_input_job",
    }
    client_root = tmp_path / "client"
    client_root.mkdir()

    class Client:
        root = client_root

        def status(self, supplied_job_id):
            assert supplied_job_id == job_id
            return {"state": "succeeded", "input_digest": result["input_digest"]}

        def materialize_artifact(self, _artifact_id, descriptor):
            return Path(descriptor["local_path"])

    destination = client_root / "data" / "remote-appearance" / job_id
    local = materialize_appearance_result(Client(), job_id, published, destination)
    assert Path(local["index_path"]) == destination / "appearance-result.json"
    assert {path.name for path in destination.iterdir()} == {
        item["name"] for item in published["result_artifacts"]}
    for item in published["result_artifacts"]:
        assert hashlib.sha256((destination / item["name"]).read_bytes()).hexdigest() == item["sha256"]

    # An absolute path in the exact audit copy is rejected before any result
    # publication; it must not be silently redacted and break the job hash.
    audit_path = render_dir / "input-job.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["provenance"]["local_path"] = "/server/private/asset.coatasset"
    audit_path.write_text(canonical(audit), encoding="utf-8")
    with pytest.raises(ValueError):
        attach_appearance_output_artifacts(service_root, job_id, result)


def test_remote_cuda_selection_shows_mixed_precision_and_unqualified_warning(tmp_path):
    """Only the remote device selector chooses backend; no local GPU probe leaks."""
    from tests import qt_test_guard  # noqa: F401
    from PySide6.QtGui import QGuiApplication
    from coat_sim.ui.appearance import AppearanceWindowController
    from tests.test_utility_windows_compact_chrome_ui import (
        _appearance_context, _create_page, _find_item,
    )

    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    controller = AppearanceWindowController(_appearance_context(), root=tmp_path)
    target = controller.computeTarget
    target._generation += 1  # Isolate this UI fixture from an async local probe.
    controller._backend = "gpu_cuda"
    controller._precision = "mixed"
    target._host_id = "remote:fixture"
    target._device_id = ""
    target._devices = []
    target._ready = False
    target.changed.emit()
    assert controller.backend == "cpu"
    assert controller.precision == "native"
    assert not controller.canStart
    target._device_id = "cuda:default"
    target._devices = [{"id": "cuda:default", "backend": "gpu_cuda",
                        "available": True, "label": "NVIDIA CUDA 默认设备"}]
    target._ready = True
    target.changed.emit()
    runtime = _create_page(app, "appearance", "light", None, controller)
    window = runtime["window"]
    try:
        workspace = _find_item(window.contentItem(), "appearanceWorkspace")
        assert workspace is not None
        workspace.setProperty("selectedCategory", "compute")
        app.processEvents()

        def item(name):
            value = _find_item(window.contentItem(), name)
            assert value is not None, name
            return value

        assert controller.backend == "gpu"
        assert controller.precision == "mixed"
        assert not item("appearanceBackendSelector").isVisible()
        assert item("appearanceGpuPrecisionSelector").isVisible()
        assert item("appearanceGpuPrecisionSelector").isEnabled()
        assert item("appearanceGpuQualificationWarning").isVisible()
        assert not item("appearanceGpuCapabilityMessage").isVisible()
        controller.setPrecision("float32")
        app.processEvents()
        assert controller.precision == "float32"
    finally:
        window.close()
        controller.shutdown()
        app.processEvents()


def test_real_remote_child_cpu_route_binds_device_and_publishes_wrapper(tmp_path, monkeypatch):
    import json
    from coat_sim.scheduler.jobs import canonical
    from coat_sim.scheduler.remote_client import make_remote_client
    from coat_sim.ui import appearance_remote_worker
    from coat_sim.ui.appearance_worker import _appearance_context_from_asset
    from tests.test_appearance_compute_entrypoints import (
        _render_inputs, _write_synthetic_v7_asset,
    )
    from tests.test_remote_server import running

    source = _write_synthetic_v7_asset(tmp_path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    context = _appearance_context_from_asset(source, tmp_path, digest)
    job_id = "a" * 32
    job_dir = tmp_path / "data" / "tmp" / "appearance" / job_id
    job_dir.mkdir(parents=True)
    input_path = job_dir / "worker_input.json"
    input_path.write_text(canonical({
        "schema": "coat_sim.appearance_remote_worker_input", "version": 1,
        "job_id": job_id, "input_sha256": "b" * 64,
        "context_sha256": hashlib.sha256(canonical(context).encode()).hexdigest(),
        "context": context, "render_inputs": _render_inputs(),
        "backend": "cpu", "precision": "native",
        "geometry_thickness_mode": "explicit_override",
        "geometry_thickness_mm": 1.0,
        "host_id": "remote:fixture", "device_id": "cpu:default",
    }), encoding="utf-8")
    with running(tmp_path / "server") as service:
        client = make_remote_client(
            service.url, tmp_path, token="ephemeral-test-operator-token",
            allow_loopback_http=True, device_id="cpu:default",
        )

        def selected(root, host_id, device_id):
            assert (Path(root), host_id, device_id) == (
                tmp_path.resolve(), "remote:fixture", "cpu:default")
            assert client.transport.device_id == device_id
            return client

        monkeypatch.setattr(appearance_remote_worker, "_settings_client", selected)
        assert appearance_remote_worker.run(
            input_path, job_dir, job_dir / "cancel.request") == 0
        wrapper = json.loads((job_dir / "appearance_result.json").read_text(encoding="utf-8"))
        assert wrapper["backend"] == "cpu" and wrapper["precision"] == "native"
        assert wrapper["job_id"] == job_id
        assert (job_dir / wrapper["image"]).is_file()
        assert client.status(job_id)["executed_device_id"] == "cpu:default"

        # A lost POST acknowledgement cannot cause a same-ID but different
        # portable request to be mistaken for this UI attempt.
        other_id = "c" * 32
        other_dir = job_dir.parent / other_id
        other_dir.mkdir()
        other_input = other_dir / "worker_input.json"
        supplied = json.loads(input_path.read_text(encoding="utf-8"))
        supplied["job_id"] = other_id
        other_input.write_text(canonical(supplied), encoding="utf-8")

        class ConflictingLostAck:
            transport = client.transport

            def capabilities(self):
                return client.capabilities()

            def submit(self, *_args, **_kwargs):
                raise RuntimeError("simulated lost acknowledgement")

            def status(self, supplied_job_id):
                return {"job_id": supplied_job_id, "kind": "appearance_render",
                        "requested_device_id": "cpu:default",
                        "input_summary": {"external_spec_sha256": "f" * 64}}

        monkeypatch.setattr(
            appearance_remote_worker, "_settings_client",
            lambda *_args: ConflictingLostAck(),
        )
        assert appearance_remote_worker.run(
            other_input, other_dir, other_dir / "cancel.request") == 2
        assert not (other_dir / "appearance_result.json").exists()

        accepted_id = "d" * 32
        accepted_dir = job_dir.parent / accepted_id
        accepted_dir.mkdir()
        accepted_input = accepted_dir / "worker_input.json"
        supplied["job_id"] = accepted_id
        accepted_input.write_text(canonical(supplied), encoding="utf-8")

        class ConflictingReceipt(ConflictingLostAck):
            def submit(self, *_args, **_kwargs):
                return {"job_id": accepted_id, "kind": "appearance_render",
                        "requested_device_id": "cpu:default",
                        "input_summary": {"external_spec_sha256": "f" * 64}}

        monkeypatch.setattr(
            appearance_remote_worker, "_settings_client",
            lambda *_args: ConflictingReceipt(),
        )
        assert appearance_remote_worker.run(
            accepted_input, accepted_dir, accepted_dir / "cancel.request") == 2
        assert not (accepted_dir / "appearance_result.json").exists()

