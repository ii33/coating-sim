"""Isolated loopback contracts for the additive v1 device-binding extension."""
from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest

from coat_sim.scheduler.jobs import canonical
from coat_sim.scheduler.remote_client import RemoteServiceError

# Reuse the existing owned-process harness. It binds only 127.0.0.1 on an
# ephemeral port, and owns shutdown.
_harness_path = Path(__file__).with_name("test_remote_server.py")
_harness_spec = importlib.util.spec_from_file_location("_remote_server_test_harness", _harness_path)
assert _harness_spec and _harness_spec.loader
_harness = importlib.util.module_from_spec(_harness_spec)
_harness_spec.loader.exec_module(_harness)
client, frozen_film, running = _harness.client, _harness.frozen_film, _harness.running


_REQUIRED_CAPABILITY_VERSIONS = {
    "protocol_version": 1,
    "api_version": 1,
    "executor_schema_version": 1,
    "device_schema_version": 1,
    "device_binding_version": 1,
}


def _bound_spec(root: Path, device_id: str = "cpu:default", *,
                schema_version: int = 1) -> dict:
    # Use an executor advertised for CPU so binding failures are not masked by
    # the unrelated executor-not-supported rejection.
    spec = deepcopy(frozen_film(root))
    spec["inputs"]["device_binding"] = {
        "schema_version": schema_version,
        "device_id": device_id,
    }
    return spec


def _post_bound_job(cli, job_id: str, *, spec: dict,
                    envelope: dict | None = None) -> dict:
    """Send the exact new six-field API form through the loopback transport."""
    frozen = canonical(spec).encode("utf-8")
    blob = cli.transport.upload_blob(frozen)
    body = {
        "api_version": 1,
        "executor_schema_version": 1,
        "device_binding_version": 1,
        "device_id": spec["inputs"]["device_binding"]["device_id"],
        "job_id": job_id,
        "spec_blob": blob,
    }
    if envelope:
        body.update(envelope)
    return cli.transport._json_call("POST", "/api/v1/jobs", body)


def test_film_serializer_accepts_solver_schema_and_rejects_unknown_fields(tmp_path):
    from coat_sim.core.film_optimization_job import deserialize_request, serialize_request

    payload = frozen_film(tmp_path / "serializer-input")["parameters"]["payload"]["request"]
    parsed = deserialize_request(payload)
    assert serialize_request(parsed) == payload

    unsupported = deepcopy(payload)
    unsupported["unrecognized_solver_option"] = True
    with pytest.raises(ValueError, match="request fields are invalid"):
        deserialize_request(unsupported)

    invalid_solver = deepcopy(payload)
    invalid_solver["solver"] = "unsupported-solver"
    with pytest.raises(ValueError, match="solver must be one of"):
        deserialize_request(invalid_solver)


def test_capabilities_negotiate_versions_and_stable_cpu_device(tmp_path):
    with running(tmp_path / "server") as service:
        cli = client(service, tmp_path / "client")
        capabilities = cli.transport.capabilities()

        for key, value in _REQUIRED_CAPABILITY_VERSIONS.items():
            assert capabilities.get(key) == value

        rows = capabilities.get("devices")
        assert isinstance(rows, list)
        cpu = [row for row in rows if row.get("id") == "cpu:default"]
        assert len(cpu) == 1
        assert str(cpu[0].get("backend", "")).casefold() == "cpu"
        assert cpu[0].get("available") is True


@pytest.mark.parametrize(
    "case",
    ("missing-envelope-version", "unsupported-envelope-version", "unsupported-spec-version"),
)
def test_device_bound_submission_rejects_missing_or_incompatible_versions(tmp_path, case):
    with running(tmp_path / case) as service:
        cli = client(service, tmp_path / (case + "-client"))
        schema_version = 2 if case == "unsupported-spec-version" else 1
        spec = _bound_spec(tmp_path / (case + "-input"), schema_version=schema_version)
        frozen = canonical(spec).encode("utf-8")
        blob = cli.transport.upload_blob(frozen)
        body = {
            "api_version": 1,
            "executor_schema_version": 1,
            "device_binding_version": 1,
            "device_id": spec["inputs"]["device_binding"]["device_id"],
            "job_id": "version-negative",
            "spec_blob": blob,
        }
        if case == "missing-envelope-version":
            # A five-field new-form request is malformed; legacy four-field
            # submissions remain a distinct, backward-compatible form.
            body.pop("device_binding_version")
        elif case == "unsupported-envelope-version":
            body["device_binding_version"] = 2

        with pytest.raises(RemoteServiceError):
            cli.transport._json_call("POST", "/api/v1/jobs", body)


def test_envelope_and_frozen_spec_device_must_match(tmp_path):
    with running(tmp_path / "server") as service:
        cli = client(service, tmp_path / "client")
        with pytest.raises(RemoteServiceError):
            _post_bound_job(
                cli,
                "device-mismatch",
                spec=_bound_spec(tmp_path / "input", "cuda:default"),
                envelope={"device_id": "cpu:default"},
            )


def test_same_job_id_with_changed_device_conflicts(tmp_path):
    with running(tmp_path / "server") as service:
        cli = client(service, tmp_path / "client")
        cpu_spec = _bound_spec(tmp_path / "input")
        first = _post_bound_job(cli, "device-idempotency", spec=cpu_spec)
        repeated = _post_bound_job(cli, "device-idempotency", spec=cpu_spec)
        assert repeated["job_id"] == first["job_id"]

        changed_device_spec = deepcopy(cpu_spec)
        changed_device_spec["inputs"]["device_binding"]["device_id"] = "cuda:default"
        with pytest.raises(RemoteServiceError) as error:
            _post_bound_job(cli, "device-idempotency", spec=changed_device_spec)
        assert error.value.code == "request_id_conflict"


def test_unsupported_device_is_rejected(tmp_path):
    device_id = "gpu:not-advertised"
    with running(tmp_path / device_id.replace(":", "-")) as service:
        cli = client(service, tmp_path / (device_id.replace(":", "-") + "-client"))
        capabilities = cli.transport.capabilities()
        rows = capabilities.get("devices", [])
        assert all(row.get("id") != device_id for row in rows)

        with pytest.raises(RemoteServiceError):
            _post_bound_job(
                cli,
                "unsupported-device",
                spec=_bound_spec(tmp_path / (device_id.replace(":", "-") + "-input"), device_id),
            )

