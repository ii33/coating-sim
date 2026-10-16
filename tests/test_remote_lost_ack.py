"""Lost-submit-ack reconciliation against an owned loopback service."""
from __future__ import annotations

import json
import sqlite3
from urllib.parse import urlsplit

import pytest

from tests import test_remote_server as remote_test
from coat_sim.scheduler.remote_client import RemoteServiceError


DEVICE_ID = "cpu:default"


def _bound_client(service, root, device_id=DEVICE_ID):
    client = remote_test.client(service, root)
    # `remote_test.client` supplies the isolated cache, loopback-only HTTP opt-in,
    # ephemeral token, and request tracing used by the other integration tests.
    client.transport.device_id = device_id
    return client


def _drop_first_successful_job_ack(client, *, after_success=None):
    """Drop the response only after the server has returned a successful POST."""
    original = client.transport._open
    trace = []
    dropped = {"value": False}

    def lose_ack(request, **options):
        path = urlsplit(request.full_url).path
        is_job_post = request.get_method() == "POST" and path == "/api/v1/jobs"
        rpc = None
        if request.get_method() == "POST" and path == "/api/v1/rpc":
            try:
                body = json.loads(request.data.decode("utf-8"))
                rpc = (body.get("action"), body.get("arguments", {}).get("job_id"))
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        if is_job_post:
            trace.append(("post_start", None))
        elif rpc is not None:
            trace.append(("rpc", rpc))

        response = original(request, **options)
        if is_job_post and not dropped["value"]:
            assert response.status == 200
            dropped["value"] = True
            trace.append(("successful_ack_dropped", response.status))
            response.close()
            if after_success is not None:
                after_success()
            raise RemoteServiceError("remote_transport_error")
        return response

    client.transport._open = lose_ack
    return dropped, trace


def _assert_one_persisted_job(root, job_id):
    database = root / "artifacts/runtime/compute-tasks.sqlite3"
    with sqlite3.connect(database) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM scheduler_specs WHERE job_id=?", (job_id,)
        ).fetchone()[0]
    assert count == 1


def _assert_reconciled_by_status(trace, job_id):
    dropped_at = next(i for i, item in enumerate(trace)
                      if item[0] == "successful_ack_dropped")
    assert any(item[0] == "rpc" and item[1] == ("status", job_id)
               for item in trace[dropped_at + 1:])


def test_lost_submit_ack_reconciles_same_id_and_device(tmp_path):
    spec = remote_test.frozen_process(tmp_path / "frozen-input")
    job_id = "lost-ack-same-device"

    with remote_test.running(tmp_path / "server") as service:
        client = _bound_client(service, tmp_path / "client")
        client.ensure_started()
        dropped, trace = _drop_first_successful_job_ack(client)

        receipt = client.submit(spec, job_id=job_id)

        assert dropped["value"]
        assert receipt["job_id"] == job_id
        assert receipt["requested_device_id"] == DEVICE_ID
        _assert_reconciled_by_status(trace, job_id)
        assert client.status(job_id)["requested_device_id"] == DEVICE_ID
        _assert_one_persisted_job(service.root, job_id)

        # A later retry with the same canonical spec and device remains idempotent.
        again = client.submit(spec, job_id=job_id)
        assert again["job_id"] == job_id
        assert again["requested_device_id"] == DEVICE_ID
        _assert_one_persisted_job(service.root, job_id)


def test_same_job_id_with_changed_device_conflicts(tmp_path):
    spec = remote_test.frozen_process(tmp_path / "frozen-input")
    job_id = "lost-ack-device-conflict"

    with remote_test.running(tmp_path / "server") as service:
        original = _bound_client(service, tmp_path / "client-original", DEVICE_ID)
        original.submit(spec, job_id=job_id)

        changed = _bound_client(service, tmp_path / "client-changed", "cpu:other")
        with pytest.raises(RemoteServiceError) as caught:
            changed.submit(spec, job_id=job_id)

        assert caught.value.code == "request_id_conflict"
        _assert_one_persisted_job(service.root, job_id)


def test_lost_ack_record_reconciles_after_service_restart(tmp_path):
    root = tmp_path / "server"
    spec = remote_test.frozen_process(tmp_path / "frozen-input")
    job_id = "lost-ack-after-restart"

    service = remote_test.OwnedService(root)
    service_closed = False
    port = service.port
    try:
        def stop_after_admission():
            nonlocal service_closed
            service.close()
            service_closed = True

        port = service.port
        client = _bound_client(service, tmp_path / "client-before-restart")
        client.ensure_started()
        dropped, trace = _drop_first_successful_job_ack(client, after_success=stop_after_admission)

        with pytest.raises(RemoteServiceError) as caught:
            client.submit(spec, job_id=job_id)

        assert dropped["value"]
        assert caught.value.code == "remote_submission_unknown"
        assert caught.value.job_id == job_id
        _assert_reconciled_by_status(trace, job_id)
        _assert_one_persisted_job(root, job_id)

        with remote_test.running(root, port=port) as restarted:
            resumed = _bound_client(restarted, tmp_path / "client-after-restart")
            status = resumed.status(job_id)
            assert status["job_id"] == job_id
            assert status["requested_device_id"] == DEVICE_ID

            replay = resumed.submit(spec, job_id=job_id)
            assert replay["job_id"] == job_id
            assert replay["requested_device_id"] == DEVICE_ID
            _assert_one_persisted_job(root, job_id)
    finally:
        if not service_closed:
            service.close()
