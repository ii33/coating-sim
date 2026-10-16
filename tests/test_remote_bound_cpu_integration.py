"""Real CPU device-bound jobs on the owned remote-service loopback harness."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import time

import pytest

from coat_sim.scheduler.jobs import canonical
from coat_sim.scheduler.remote_client import RemoteServiceError


# Reuse the existing test harness and fixtures. Its child owns the service,
# binds only 127.0.0.1 on an ephemeral port, and shuts down at context exit.
_harness_path = Path(__file__).with_name("test_remote_server.py")
_harness_spec = importlib.util.spec_from_file_location("_bound_cpu_server_test_harness", _harness_path)
assert _harness_spec and _harness_spec.loader
_harness = importlib.util.module_from_spec(_harness_spec)
_harness_spec.loader.exec_module(_harness)
client = _harness.client
frozen_film = _harness.frozen_film
frozen_process = _harness.frozen_process
running = _harness.running
wait_state = _harness.wait_state


def _bound_spec(spec: dict, device_id: str = "cpu:default") -> dict:
    value = deepcopy(spec.to_dict() if hasattr(spec, "to_dict") else spec)
    value["inputs"]["device_binding"] = {
        "schema_version": 1,
        "device_id": device_id,
    }
    return value


def _post_bound_job(cli, job_id: str, spec: dict) -> dict:
    from coat_sim.scheduler.remote_contract import prepare_portable_spec

    spec = prepare_portable_spec(spec).to_dict()
    frozen = canonical(spec).encode("utf-8")
    blob = cli.transport.upload_blob(frozen)
    return cli.transport._json_call("POST", "/api/v1/jobs", {
        "api_version": 1,
        "executor_schema_version": 1,
        "device_binding_version": 1,
        "device_id": spec["inputs"]["device_binding"]["device_id"],
        "job_id": job_id,
        "spec_blob": blob,
    })


def _appearance_spec(*, device_id="cuda:default", backend="gpu_cuda", precision="mixed"):
    return {
        "protocol_version": 1,
        "executor": "appearance_render",
        "parameters": {"request": {
            "render_inputs": {},
            "backend": backend,
            "precision": precision,
        }},
        "inputs": {
            "appearance_artifact": {
                "artifact_id": "a" * 64,
                "sha256": "b" * 64,
            },
            "device_binding": {
                "schema_version": 1,
                "device_id": device_id,
            },
        },
    }


def test_real_bound_cpu_jobs_attest_download_and_survive_restart(tmp_path):
    server_root = tmp_path / "server"
    with running(server_root) as service:
        cli = client(service, tmp_path / "client-cache")
        capabilities = cli.transport.capabilities()
        assert capabilities["device_binding_version"] == 1
        cpu = next(row for row in capabilities["devices"] if row["id"] == "cpu:default")
        assert cpu["available"] is True
        assert {"process_recipe", "film_optimization"}.issubset(cpu["executors"])

        deposition = _bound_spec(frozen_process(tmp_path / "deposition-inputs"))
        receipt = _post_bound_job(cli, "bound-cpu-deposition", deposition)
        assert receipt["requested_device_id"] == "cpu:default"
        final = wait_state(cli, "bound-cpu-deposition", {"succeeded", "failed"})
        assert final["state"] == "succeeded", final
        assert final["requested_device_id"] == "cpu:default"
        assert final["executed_device_id"] == "cpu:default"

        result = cli.result("bound-cpu-deposition")
        bundle = next(item for item in result["result_artifacts"] if item["role"] == "bundle")
        local_bundle = cli.materialize_artifact(bundle["artifact_id"], bundle)
        actual_sha256 = hashlib.sha256(local_bundle.read_bytes()).hexdigest()
        assert local_bundle.stat().st_size == bundle["bytes"]
        assert actual_sha256 == bundle["sha256"]
        assert local_bundle.is_relative_to(tmp_path / "client-cache")

        film = _bound_spec(frozen_film(tmp_path / "film-inputs"))
        film_receipt = _post_bound_job(cli, "bound-cpu-film", film)
        assert film_receipt["requested_device_id"] == "cpu:default"
        film_final = wait_state(cli, "bound-cpu-film", {"succeeded", "failed"})
        assert film_final["state"] == "succeeded", film_final
        assert film_final["requested_device_id"] == "cpu:default"
        # The film result does not expose an actual backend attestation yet;
        # the service must not promote the requested CPU choice into a claim.
        assert "executed_device_id" not in film_final
        assert cli.result("bound-cpu-film")["kind"] == "coat_sim.film_optimization_result"

        changed_device = deepcopy(deposition)
        changed_device["inputs"]["device_binding"]["device_id"] = "cuda:default"
        with pytest.raises(RemoteServiceError) as error:
            _post_bound_job(cli, "bound-cpu-deposition", changed_device)
        assert error.value.code == "request_id_conflict"

        persisted_spec = server_root / "artifacts/runtime/scheduler/jobs/bound-cpu-deposition/spec.json"
        assert json.loads(persisted_spec.read_text(encoding="utf-8"))["inputs"]["device_binding"] == {
            "schema_version": 1,
            "device_id": "cpu:default",
        }
        port = service.port

    with running(server_root, port=port) as restarted:
        resumed = client(restarted, tmp_path / "restarted-client-cache")
        deposition_after_restart = resumed.status("bound-cpu-deposition")
        assert deposition_after_restart["state"] == "succeeded"
        assert deposition_after_restart["requested_device_id"] == "cpu:default"
        assert deposition_after_restart["executed_device_id"] == "cpu:default"
        film_after_restart = resumed.status("bound-cpu-film")
        assert film_after_restart["state"] == "succeeded"
        assert film_after_restart["requested_device_id"] == "cpu:default"
        assert "executed_device_id" not in film_after_restart


_INVALIDATABLE_CHILD = r'''import sys, threading, os, json
from pathlib import Path
from coat_sim.scheduler.remote_server import RemoteComputeService
service = RemoteComputeService(sys.argv[1], host="127.0.0.1", port=int(sys.argv[2]),
    insecure_loopback=sys.argv[3] == "loopback", allow_probe=sys.argv[3] == "loopback",
    allow_insecure_http=sys.argv[3] == "authenticated-http",
    token="ephemeral-test-operator-token")
invalidated = [False]
original_capabilities = service._capabilities
def capabilities():
    value = original_capabilities()
    if invalidated[0]:
        for row in value.get("devices", []):
            if row.get("id") == "cpu:default":
                row["available"] = False
                row["reason"] = "test-only capability invalidation"
    return value
service._capabilities = capabilities
def watch():
    for line in sys.stdin:
        if line.strip() == "invalidate":
            invalidated[0] = True
        else:
            service.scheduler.stopping.set()
            return
threading.Thread(target=watch, daemon=True).start()
Path(sys.argv[1], 'startup-' + sys.argv[4] + '.json').write_text(json.dumps({
    'nonce': sys.argv[4], 'pid': os.getpid(), 'instance': service.scheduler.owner}), encoding='utf-8')
service.run()
'''


@contextmanager
def _running_with_capability_invalidation(root):
    previous_child = _harness._CHILD
    _harness._CHILD = _INVALIDATABLE_CHILD
    try:
        with running(root) as service:
            yield service
    finally:
        _harness._CHILD = previous_child


_CUDA_PROBE_CHILD_TEMPLATE = r'''import sys, threading, os, json
from pathlib import Path
from coat_sim.scheduler import remote_appearance
invalidated = [__INITIAL_UNAVAILABLE__]
def fake_cuda_probe(root):
    if invalidated[0]:
        return False, "test_cuda_probe_failed"
    return True, "test_cuda_allocation_verified"
remote_appearance.probe_appearance_cuda_runtime = fake_cuda_probe
from coat_sim.scheduler.remote_server import RemoteComputeService
service = RemoteComputeService(sys.argv[1], host="127.0.0.1", port=int(sys.argv[2]),
    insecure_loopback=sys.argv[3] == "loopback", allow_probe=sys.argv[3] == "loopback",
    allow_insecure_http=sys.argv[3] == "authenticated-http",
    token="ephemeral-test-operator-token")
def watch():
    for line in sys.stdin:
        if line.strip() == "invalidate-cuda":
            invalidated[0] = True
            Path(sys.argv[1], ".test-cuda-invalidated").write_text("1", encoding="ascii")
        else:
            service.scheduler.stopping.set()
            return
threading.Thread(target=watch, daemon=True).start()
Path(sys.argv[1], 'startup-' + sys.argv[4] + '.json').write_text(json.dumps({
    'nonce': sys.argv[4], 'pid': os.getpid(), 'instance': service.scheduler.owner}), encoding='utf-8')
service.run()
'''


@contextmanager
def _running_with_cuda_probe(root, *, initially_unavailable):
    previous_child = _harness._CHILD
    _harness._CHILD = _CUDA_PROBE_CHILD_TEMPLATE.replace(
        "__INITIAL_UNAVAILABLE__", repr(bool(initially_unavailable)))
    try:
        with running(root) as service:
            yield service
    finally:
        _harness._CHILD = previous_child


def test_server_rechecks_device_availability_after_capability_change(tmp_path):
    with _running_with_capability_invalidation(tmp_path / "server") as service:
        cli = client(service, tmp_path / "client")
        initial = cli.transport.capabilities()
        cpu = next(row for row in initial["devices"] if row["id"] == "cpu:default")
        assert cpu["available"] is True

        service.process.stdin.write(b"invalidate\n")
        service.process.stdin.flush()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = cli.transport.capabilities()
            cpu = next(row for row in current["devices"] if row["id"] == "cpu:default")
            if cpu["available"] is False:
                break
            time.sleep(0.05)
        assert cpu["available"] is False

        spec = _bound_spec(frozen_film(tmp_path / "film-inputs"))
        with pytest.raises(RemoteServiceError) as error:
            _post_bound_job(cli, "stale-capability-device", spec)
        assert error.value.code == "device_unavailable"


def test_cuda_contract_rejects_mismatch_and_disabled_probe(tmp_path):
    with _running_with_cuda_probe(tmp_path / "server", initially_unavailable=True) as service:
        cli = client(service, tmp_path / "client")
        capabilities = cli.transport.capabilities()
        cuda = next(row for row in capabilities["devices"] if row["id"] == "cuda:default")
        assert cuda["available"] is False
        assert cuda["reason"] == "test_cuda_probe_failed"

        # A CUDA binding cannot request the CPU/native appearance path.
        mismatched = _appearance_spec(device_id="cuda:default", backend="cpu", precision="native")
        with pytest.raises(RemoteServiceError) as mismatch_error:
            _post_bound_job(cli, "cuda-request-mismatch", mismatched)
        assert mismatch_error.value.code == "invalid_appearance_request"

        # Even a valid CUDA request is not admitted when its isolated runtime
        # allocation probe reports unavailable.
        with pytest.raises(RemoteServiceError) as unavailable_error:
            _post_bound_job(cli, "cuda-probe-disabled", _appearance_spec())
        assert unavailable_error.value.code == "device_unavailable"


def test_cuda_allocation_is_reprobed_before_post_admission(tmp_path):
    server_root = tmp_path / "server"
    with _running_with_cuda_probe(server_root, initially_unavailable=False) as service:
        cli = client(service, tmp_path / "client")
        initial = cli.transport.capabilities()
        cuda = next(row for row in initial["devices"] if row["id"] == "cuda:default")
        assert cuda["available"] is True

        service.process.stdin.write(b"invalidate-cuda\n")
        service.process.stdin.flush()
        marker = server_root / ".test-cuda-invalidated"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not marker.is_file():
            time.sleep(0.05)
        assert marker.is_file()

        with pytest.raises(RemoteServiceError) as error:
            _post_bound_job(cli, "cuda-stale-capability", _appearance_spec())
        assert error.value.code == "device_unavailable"
        updated = cli.transport.capabilities()
        cuda = next(row for row in updated["devices"] if row["id"] == "cuda:default")
        assert cuda["available"] is False

