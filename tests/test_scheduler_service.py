"""Lifecycle regressions for the process-independent compute scheduler.

These tests use only the local ``probe`` executor.  They deliberately exercise
the public client API rather than scheduler internals so GUI, MCP, and CLI can
share the same service contract.
"""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from coat_sim.scheduler import SchedulerClient


@contextmanager
def scheduler_root() -> Iterator[tuple[Path, SchedulerClient]]:
    """Give every integration test an isolated, disposable scheduler state."""
    import tempfile

    with tempfile.TemporaryDirectory(
        prefix="coat-scheduler-test-", ignore_cleanup_errors=True
    ) as directory:
        client = SchedulerClient(Path(directory))
        try:
            yield Path(directory), client
        finally:
            # stop() is explicit by design: dropping a client must not stop work.
            try:
                client.stop()
            except (OSError, RuntimeError):
                # Preserve the original test failure if the daemon already died.
                pass


def job_spec(
    *, delay_s: float = 0.05, payload_bytes: int = 0,
    crash: bool = False, fail_save: bool = False,
    inputs: dict | None = None,
) -> dict:
    return {
        "protocol_version": 1,
        "executor": "probe",
        "parameters": {
            "delay_s": delay_s,
            "payload_bytes": payload_bytes,
            "crash": crash,
            "fail_save": fail_save,
        },
        "inputs": {} if inputs is None else inputs,
    }


def wait_state(client: SchedulerClient, job_id: str, *states: str,
               timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    latest = client.status(job_id)
    while latest.get("state") not in states:
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"job {job_id} did not reach {states}; last status: {latest!r}"
            )
        time.sleep(0.025)
        latest = client.status(job_id)
    return latest


def wait_progress(client: SchedulerClient, job_id: str, fraction: float,
                  timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    latest = client.status(job_id)
    while True:
        progress = latest.get("progress") or {}
        if float(progress.get("fraction", 0)) >= fraction:
            return latest
        if latest.get("state") in {"succeeded", "failed", "cancelled"}:
            raise AssertionError(
                f"job {job_id} became terminal before progress {fraction}: {latest!r}"
            )
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"job {job_id} did not reach progress {fraction}; last status: {latest!r}"
            )
        time.sleep(0.025)
        latest = client.status(job_id)


def stop_service(client: SchedulerClient) -> None:
    """Stop explicitly and allow the daemon's process handle to settle."""
    from coat_sim.core.process_identity import _process_identity

    endpoint = client.root / "artifacts" / "runtime" / "scheduler" / "endpoint.json"
    try:
        pid = json.loads(endpoint.read_text(encoding="utf-8")).get("pid")
    except (OSError, ValueError):
        pid = None
    if not isinstance(pid, int):
        client.stop()
        return
    initial_state, initial_start = _process_identity(pid)
    client.stop()
    if initial_state == "dead":
        return
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        current_state, current_start = _process_identity(pid)
        if current_state == "dead" or (
            initial_start and current_start and current_start != initial_start
        ):
            return
        time.sleep(0.025)
    raise AssertionError(
        f"scheduler daemon PID {pid} with creation identity {initial_start!r} "
        "did not exit after stop()"
    )


def test_submitting_client_process_exit_does_not_cancel_job() -> None:
    with scheduler_root() as (root, observer):
        # A real short-lived client process makes this stronger than dropping a
        # Python object: its process exits while the independent daemon owns work.
        script = r"""
import json, sys
from pathlib import Path
from coat_sim.scheduler import SchedulerClient
client = SchedulerClient(Path(sys.argv[1]))
spec = {"protocol_version": 1, "executor": "probe",
        "parameters": {"delay_s": 0.45, "payload_bytes": 0,
                       "crash": False, "fail_save": False},
        "inputs": {}}
print(json.dumps(client.submit(spec)), flush=True)
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(root)],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        submitted = json.loads(completed.stdout.strip().splitlines()[-1])
        job_id = submitted["job_id"]

        final = wait_state(observer, job_id, "succeeded", "failed", "cancelled")
        assert final["state"] == "succeeded"


def test_second_client_can_read_result_and_cancel_another_job() -> None:
    with scheduler_root() as (_, first):
        second = SchedulerClient(first.root)
        done = first.submit(job_spec(delay_s=0.08))
        assert wait_state(second, done["job_id"], "succeeded", "failed")["state"] == "succeeded"
        result = second.result(done["job_id"])
        assert result is not None
        assert result.get("job_id", done["job_id"]) == done["job_id"]

        pending = first.submit(job_spec(delay_s=2.0))
        # Exercise cancellation after admission to the active worker, not only
        # the fast queued-to-cancelled path.
        wait_state(second, pending["job_id"], "running")
        cancelled = second.cancel(pending["job_id"])
        assert cancelled["state"] in {"running", "cancelled"}, cancelled
        assert cancelled.get("cancel_requested") is True, cancelled
        final = wait_state(second, pending["job_id"], "cancelled", "succeeded", "failed")
        assert final["state"] == "cancelled"
        # Cancellation must release the worker slot so the queue can continue.
        following = first.submit(job_spec(delay_s=0.04))
        resumed = wait_state(second, following["job_id"], "succeeded", "failed", "cancelled")
        assert resumed["state"] == "succeeded", resumed


def test_same_id_is_idempotent_and_different_spec_conflicts() -> None:
    with scheduler_root() as (_, client):
        original = job_spec(delay_s=0.2)
        first = client.submit(original, job_id="stable-probe-id")
        duplicate = client.submit(original, job_id="stable-probe-id")
        assert duplicate["job_id"] == first["job_id"]
        assert duplicate["state"] == first["state"] or duplicate["state"] in {
            "queued", "running", "succeeded"
        }

        changed = job_spec(delay_s=0.3)
        try:
            client.submit(changed, job_id="stable-probe-id")
        except ValueError as exc:
            assert "request_id_conflict" in str(exc)
        else:
            raise AssertionError("different parameters for the same ID were accepted")


def test_continuous_submissions_do_not_corrupt_existing_jobs() -> None:
    with scheduler_root() as (_, client):
        submitted = [client.submit(job_spec(delay_s=0.04)) for _ in range(12)]
        ids = [entry["job_id"] for entry in submitted]
        assert len(set(ids)) == len(ids)
        results = [wait_state(client, job_id, "succeeded", "failed") for job_id in ids]
        assert all(item["state"] == "succeeded" for item in results)
        events = client.snapshot(after_event=0, limit=500)
        assert events


def test_restart_resumes_queued_jobs_and_marks_running_job_interrupted() -> None:
    with scheduler_root() as (root, client):
        running = client.submit(job_spec(delay_s=20.0))
        running_status = wait_state(client, running["job_id"], "running")
        queued = client.submit(job_spec(delay_s=0.04))
        assert queued["state"] == "queued"

        stop_service(client)
        restarted = SchedulerClient(root)
        try:
            restarted.ensure_started()
            interrupted = wait_state(restarted, running["job_id"], "failed")
            assert interrupted.get("interrupted") is True
            assert interrupted.get("error_code") == "scheduler_interrupted"
            resumed = wait_state(restarted, queued["job_id"], "succeeded", "failed")
            assert resumed["state"] == "succeeded"
            assert running_status["state"] == "running"
        finally:
            stop_service(restarted)


def test_compute_crash_and_result_save_failure_have_distinct_causes() -> None:
    with scheduler_root() as (_, client):
        for parameters, expected_code in [
            ({"crash": True, "fail_save": False}, "worker_crashed"),
            ({"crash": False, "fail_save": True}, "result_save_failed"),
        ]:
            with unittest.TestCase().subTest(expected_code=expected_code):
                submitted = client.submit(job_spec(**parameters))
                final = wait_state(client, submitted["job_id"], "failed", "succeeded")
                assert final["state"] == "failed"
                assert final.get("error_code") == expected_code
                assert final.get("error")


def test_cancel_race_reaches_one_stable_terminal_state() -> None:
    with scheduler_root() as (_, client):
        submitted = client.submit(job_spec(delay_s=0.06))
        first = client.cancel(submitted["job_id"])
        final = wait_state(client, submitted["job_id"], "cancelled", "succeeded", "failed")
        repeated = client.cancel(submitted["job_id"])
        assert final["state"] in {"cancelled", "succeeded"}
        assert repeated["state"] == final["state"]
        assert first["state"] in {"running", "cancelled", "succeeded"}
        if first["state"] == "running":
            assert first.get("cancel_requested") is True


def test_near_completion_duplicate_cancel_emits_one_terminal_event() -> None:
    with scheduler_root() as (root, first_client):
        second_client = SchedulerClient(root)
        submitted = first_client.submit(job_spec(delay_s=2.5))
        wait_state(first_client, submitted["job_id"], "running")
        near_done = wait_progress(first_client, submitted["job_id"], 0.8)
        assert near_done["state"] == "running"
        cursor = first_client.events(after_sequence=0, wait_s=0)["sequence"]

        first_cancel = first_client.cancel(submitted["job_id"])
        assert first_cancel.get("cancel_requested") is True
        second_cancel = second_client.cancel(submitted["job_id"])
        assert second_cancel["job_id"] == submitted["job_id"]
        final = wait_state(
            first_client, submitted["job_id"], "cancelled", "succeeded", "failed"
        )
        assert final["state"] == "cancelled"

        page = first_client.events(after_sequence=cursor, wait_s=0)
        matching = [event for event in page["events"]
                    if event.get("job_id") == submitted["job_id"]]
        cancel_events = [event for event in matching
                         if event.get("type") == "cancel_requested"]
        terminal_events = [event for event in matching
                           if event.get("state") in {"succeeded", "failed", "cancelled"}]
        assert len(cancel_events) == 1
        assert len(terminal_events) == 1


def test_cancel_event_write_rollback_does_not_leave_marker_file() -> None:
    from coat_sim.scheduler.daemon import Scheduler

    import tempfile

    with tempfile.TemporaryDirectory(
        prefix="coat-scheduler-cancel-rollback-", ignore_cleanup_errors=True
    ) as directory:
        scheduler = Scheduler(Path(directory))
        job_id = "cancel-rollback-probe"
        marker = Path(directory) / "artifacts" / "runtime" / "jobs" / (job_id + ".cancel")
        try:
            scheduler.dispatch("submit", {
                "spec": job_spec(delay_s=5.0), "job_id": job_id, "metadata": {},
            })
            assert not marker.exists()
            with patch.object(
                scheduler.store,
                "_event",
                side_effect=RuntimeError("injected event persistence failure"),
            ):
                with unittest.TestCase().assertRaisesRegex(
                    RuntimeError, "injected event persistence failure"
                ):
                    scheduler.dispatch("cancel", {"job_id": job_id})
            assert not marker.exists()
            status = scheduler.store.status(job_id)
            assert status["state"] == "queued"
            assert status["cancel_requested"] is False
        finally:
            scheduler.guard.close()
            scheduler.lock.close()


def test_large_result_uses_asset_reference_and_keeps_control_plane_small() -> None:
    with scheduler_root() as (_, client):
        submitted = client.submit(job_spec(delay_s=0.03, payload_bytes=8 * 1024 * 1024))
        # Poll calls must stay responsive while the probe persists a large result.
        wait_state(client, submitted["job_id"], "running")
        started = time.monotonic()
        final = wait_state(client, submitted["job_id"], "succeeded", "failed", timeout=90)
        status_elapsed = time.monotonic() - started
        assert final["state"] == "succeeded"
        assert status_elapsed < 10
        status_wire = json.dumps(final, default=str).encode("utf-8")
        assert len(status_wire) < 128 * 1024

        result = client.result(submitted["job_id"])
        result_wire = json.dumps(result, default=str).encode("utf-8")
        assert len(result_wire) < 128 * 1024
        assert result.get("artifact_id")
        artifacts = result.get("result_artifacts")
        assert isinstance(artifacts, list) and artifacts
        assert artifacts[0].get("bytes") == 8 * 1024 * 1024
        assert artifacts[0].get("sha256")
        download = client.root / "downloaded-result.bin"
        client.fetch_artifact(result["artifact_id"], download)
        digest = hashlib.sha256()
        with download.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        assert download.stat().st_size == artifacts[0]["bytes"]
        assert digest.hexdigest() == artifacts[0]["sha256"]


def test_submitted_spec_is_frozen_and_not_written_back() -> None:
    with scheduler_root() as (_, client):
        source_inputs = {"recipe": {"steps": [1, 2, 3]}}
        spec = job_spec(delay_s=0.15, inputs=source_inputs)
        before = json.loads(json.dumps(spec))
        submitted = client.submit(spec)
        original_digest = client.status(submitted["job_id"]).get("input_digest")
        assert isinstance(original_digest, str) and len(original_digest) == 64
        source_inputs["recipe"]["steps"].append(4)
        spec["parameters"]["delay_s"] = 0
        assert spec != before
        final = wait_state(client, submitted["job_id"], "succeeded", "failed")
        assert final["state"] == "succeeded"
        assert client.status(submitted["job_id"])["input_digest"] == original_digest
        assert spec["parameters"]["delay_s"] == 0


def test_new_client_reconnects_from_event_cursor_and_snapshot_sequence() -> None:
    with scheduler_root() as (root, first_client):
        first_job = first_client.submit(job_spec(delay_s=0.3))
        first_page = first_client.events(after_sequence=0, wait_s=1.0)
        assert isinstance(first_page.get("sequence"), int)
        assert first_page["sequence"] >= 0

        # Model a client process reconnecting with its last durable event cursor.
        reconnected = SchedulerClient(root)
        second_job = reconnected.submit(job_spec(delay_s=0.04))
        next_page = reconnected.events(
            after_sequence=first_page["sequence"], wait_s=1.0
        )
        assert next_page["sequence"] >= first_page["sequence"]
        assert isinstance(next_page.get("events"), list)
        assert any(
            event.get("job_id") == second_job["job_id"]
            for event in next_page["events"]
        )

        snapshot = reconnected.snapshot(after_event=0, limit=100)
        if isinstance(snapshot, dict) and "sequence" in snapshot:
            assert snapshot["sequence"] >= next_page["sequence"]
        assert wait_state(
            reconnected, first_job["job_id"], "succeeded", "failed"
        )["state"] == "succeeded"


def test_old_event_cursor_is_reconciled_after_restart() -> None:
    with scheduler_root() as (root, client):
        submitted = client.submit(job_spec(delay_s=0.03))
        wait_state(client, submitted["job_id"], "succeeded", "failed")
        snapshot = client.snapshot(after_event=0, limit=100)
        if isinstance(snapshot, dict):
            saved_sequence = snapshot.get("sequence", 0)
        else:
            saved_sequence = 0

        stop_service(client)
        restarted = SchedulerClient(root)
        try:
            restarted.ensure_started()
            # A deliberately expired cursor must be signaled, never silently
            # treated as a complete event history.
            page = restarted.events(after_sequence=-1, wait_s=0)
            assert page.get("reset") is True
            assert isinstance(page.get("snapshot"), dict)
            assert page["snapshot"].get("sequence", 0) >= saved_sequence
        finally:
            stop_service(restarted)


def test_hot_queries_use_scheduler_memory_without_store_reads() -> None:
    from coat_sim.scheduler.daemon import Scheduler

    import tempfile

    with tempfile.TemporaryDirectory(
        prefix="coat-scheduler-cache-test-", ignore_cleanup_errors=True
    ) as directory:
        scheduler = Scheduler(Path(directory))
        try:
            scheduler.dispatch("submit", {
                "spec": job_spec(delay_s=30),
                "job_id": "memory-query-probe",
                "metadata": {},
            })

            def forbidden(*args, **kwargs):
                raise AssertionError("hot query attempted a persistent store read")

            with patch.object(scheduler.store, "status", forbidden), patch.object(
                scheduler.store.queue, "snapshot", forbidden
            ):
                detail = scheduler.dispatch("status", {"job_id": "memory-query-probe"})
                snapshot = scheduler.dispatch("snapshot", {"after_event": 0, "limit": 20})
                events = scheduler.dispatch("events", {"after_sequence": 0, "wait_s": 0})
            assert detail["state"] == "queued"
            assert snapshot.get("jobs")
            assert events.get("events")
        finally:
            scheduler.guard.close()
            scheduler.lock.close()


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None):
    """Expose the focused functional checks through stdlib unittest discovery."""
    del loader, tests, pattern
    cases = unittest.TestSuite()
    for name, candidate in sorted(globals().items()):
        if name.startswith("test_") and callable(candidate):
            cases.addTest(unittest.FunctionTestCase(candidate, description=name))
    return cases
