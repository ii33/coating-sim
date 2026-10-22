"""Adversarial integration coverage for independent film sensitivity jobs.

Only transport is in memory. The confirmed MCP route invokes the actual worker
against its real persisted specification, checkpoint and outcome files.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import os
from threading import Event
from typing import Any

import pytest

from coat_sim.core import film_sensitivity_jobs as jobs
from coat_sim.core.api import CoatService
from coat_sim.core.compute_tasks import ComputeTaskStore
from coat_sim.core.film_optimization import FilmOptimizationRequest
from coat_sim.core.film_optimization_job import deserialize_request, serialize_request
from coat_sim.core.film_sensitivity import (
    film_sensitivity_request_to_payload,
    freeze_film_sensitivity_request,
)
from coat_sim.scheduler import submission
from coat_sim.scheduler.client import SchedulerClient
from coat_sim.scheduler.io import write_json, write_text
from coat_sim.scheduler.jobs import JobSpec, canonical
from coat_sim.scheduler.store import SchedulerStore
from tests.test_film_sensitivity import problem
from tests.test_film_sensitivity_quick_controller import app as app, harness as harness


def _payload(**options):
    service, request = problem(**options)
    return film_sensitivity_request_to_payload(freeze_film_sensitivity_request(service, request))


def _call(name, arguments):
    from coat_sim.mcp import server
    response = asyncio.run(server.mcp.call_tool(name, arguments))
    for field in ("structured_content", "structuredContent"):
        value = getattr(response, field, None)
        if value is not None:
            return value
    for block in getattr(response, "content", ()) or ():
        if getattr(block, "text", None):
            return json.loads(block.text)
    raise AssertionError("registered MCP tool returned no JSON")


class _WorkerTransport:
    """Protocol peer using durable stores and the real worker, without IPC."""
    def __init__(self, root):
        self.root = root
        self.store = SchedulerStore(root)
        self.submissions: list[dict[str, Any]] = []
        self.worker_calls = 0

    def request(self, action, arguments):
        if action == "refresh_estimates":
            return {"refreshed": True}
        if action == "status":
            return self.store.status(arguments["job_id"])
        if action == "submit":
            from coat_sim.scheduler import worker
            self.submissions.append(deepcopy(arguments))
            row = self.store.submit(**arguments)
            directory = self.root / "artifacts/runtime/scheduler/jobs" / row["job_id"]
            directory.mkdir(parents=True, exist_ok=True)
            write_text(directory / "spec.json", canonical(arguments["spec"]))
            write_json(directory / "launch.json", {"instance": "review-worker"})
            self.worker_calls += 1
            assert worker.run(self.root, row["job_id"], os.getpid(), "review-worker") == 0
            outcome = json.loads((directory / "outcome.json").read_text(encoding="utf-8"))
            return self.store.transition(row["job_id"], **outcome)
        raise AssertionError(f"unexpected scheduler action: {action}")


@pytest.fixture
def mcp_peer(tmp_path, monkeypatch):
    from coat_sim.mcp import server
    service = CoatService(tmp_path)
    transport = _WorkerTransport(tmp_path)

    def factory(root=None, **_kwargs):
        return SchedulerClient(root or tmp_path, transport=transport)

    monkeypatch.setattr(server, "service", service)
    monkeypatch.setattr("coat_sim.scheduler.SchedulerClient", factory)
    monkeypatch.setattr(submission, "SchedulerClient", factory)
    monkeypatch.setattr("subprocess.Popen", lambda *_args, **_kwargs: pytest.fail("unexpected child process"))
    return service, transport


def test_registered_mcp_plan_is_readonly_and_submit_waits_for_real_confirmation(mcp_peer):
    service, peer = mcp_peer
    payload = _payload(operation="optimize")
    payload["max_evaluations"] = 2
    store = ComputeTaskStore(service.root)
    from coat_sim.mcp import server

    async def hints():
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        assert tools["plan_film_sensitivity"].annotations.read_only_hint is True
        assert tools["submit_film_sensitivity"].annotations.read_only_hint is False

    asyncio.run(hints())
    planned = _call("plan_film_sensitivity", {"payload": payload})
    assert planned["ok"] and planned["request"] == payload
    assert not store.path.exists() or store.snapshot()["confirmations"] == []
    assert not peer.submissions and peer.worker_calls == 0
    first = _call("submit_film_sensitivity", {"payload": payload, "client_request_id": "review-confirmed"})
    assert first["state"] == "prepared" and first["needs_release_request"]
    assert peer.worker_calls == 0
    assert _call("submit_film_sensitivity", {"payload": payload, "client_request_id": "review-confirmed"})["estimate_id"] == first["estimate_id"]
    pending = store.request(first["estimate_id"])
    assert pending["release_policy"] == "manual"
    accepted = submission.confirm_and_submit(service, first["estimate_id"], True)
    assert peer.worker_calls == len(peer.submissions) == 1
    assert accepted["kind"] == "film_sensitivity" and accepted["state"] == "succeeded"
    assert accepted["result"]["state"] == "partial"
    expected = peer.submissions[0]["spec"]
    assert expected["executor"] == "film_sensitivity"
    assert jobs.result_matches_job(accepted["result"], expected["parameters"]["payload"])
    assert _call("submit_film_sensitivity", {"payload": payload, "client_request_id": "review-confirmed"})["job_id"] == accepted["job_id"]
    assert peer.worker_calls == 1


@pytest.mark.parametrize("field", ["errors", "error_model", "robustness_objective", "scenarios", "operation"])
def test_ordinary_optimization_v1_rejects_sensitivity_fields(field):
    _service, request = problem()
    ordinary = serialize_request(FilmOptimizationRequest(request.design_snapshot, request.design_revision,
        request.variables, request.objectives))
    ordinary[field] = _payload()[field]
    with pytest.raises(ValueError, match="fields"):
        deserialize_request(ordinary)


@pytest.mark.parametrize("request_id", ["", False, 0])
def test_direct_submission_rejects_falsey_non_ids_before_estimation(mcp_peer, request_id):
    service, peer = mcp_peer
    with pytest.raises(ValueError, match="client_request_id"):
        jobs.submit_sensitivity(service, _payload(), request_id)
    assert not peer.submissions
    with ComputeTaskStore(service.root).connection() as db:
        assert db.execute("SELECT COUNT(*) FROM estimates").fetchone()[0] == 0


def test_discussion_actual_dispatch_cannot_submit_sensitivity_or_estimate():
    from tests.test_assistant_discussion_mode import _actual_run
    payload = _payload()
    calls = [("submit_film_sensitivity", {"payload": payload}),
             ("estimate_compute_task", {"kind": "film_sensitivity", "parameters": {"payload": payload}})]
    rpc, replies, _tools = asyncio.run(_actual_run(calls, mode="chat"))
    assert not rpc and len(replies) == 2
    assert all(reply.content["error"]["code"] == "discussion_read_only" for reply in replies)


@pytest.fixture
def scored():
    service, request = problem(operation="optimize")
    job = jobs.build_job_payload(service, replace(request, max_evaluations=2))
    checkpoints = []
    result = jobs.execute_job({"payload": job}, Event(), lambda _row: None, "memory",
        lambda _path, row: checkpoints.append(deepcopy(row)))
    return job, result, checkpoints[-1]


def _persist_job(root, job, job_id="review-job"):
    spec = JobSpec("film_sensitivity", {"payload": job}, {})
    store = SchedulerStore(root)
    store.submit(spec.to_dict(), job_id=job_id)
    directory = root / "artifacts/runtime/scheduler/jobs" / job_id
    directory.mkdir(parents=True, exist_ok=True)
    write_text(directory / "spec.json", canonical(spec.to_dict()))
    return store, directory


def test_history_rejects_valid_result_from_different_frozen_job(scored, tmp_path):
    from coat_sim.ui.compute_queue import _read_queue_result
    job, result, _checkpoint = scored
    foreign = deepcopy(job)
    foreign["request"]["design_revision"] += 1
    store, _directory = _persist_job(tmp_path, foreign)
    store.transition("review-job", state="succeeded", result=result)
    with pytest.raises(ValueError, match="identity|bound|match"):
        _read_queue_result(None, store.queue, "review-job")


def test_daemon_failed_checkpoint_recovery_keeps_failed_business_state(scored, tmp_path):
    from coat_sim.scheduler.daemon import Scheduler
    job, _result, checkpoint = scored
    _store, directory = _persist_job(tmp_path, job)
    write_json(directory / "best-candidate.json", checkpoint)
    scheduler = Scheduler(tmp_path)
    recovered = scheduler._attach_best_candidate_checkpoint("review-job", {"state": "failed", "error": "worker crash"})
    assert recovered["state"] == "failed"
    assert recovered["result"]["state"] == "failed"


def test_incomplete_checkpoint_is_not_a_viewable_terminal_result(scored):
    from coat_sim.scheduler.result_capability import job_has_viewable_result
    _job, _result, checkpoint = scored
    assert not checkpoint["result"]["is_final"]
    assert not jobs.is_sensitivity_result(checkpoint["result"])
    assert not job_has_viewable_result({"kind": "film_sensitivity", "state": "succeeded", "result": checkpoint["result"]})


@pytest.mark.qt
@pytest.mark.parametrize("overwrite", [False, True])
def test_report_write_failure_preserves_old_file_and_no_partial_new_file(harness, tmp_path, monkeypatch, overwrite):
    import coat_sim.core.film_result_service as result_service
    panel = harness.sensitivity
    assert panel.start("analyze")
    harness.pool.run()
    target = tmp_path / "report.json"
    if overwrite:
        target.write_text("keep existing complete report", encoding="utf-8")

    def fail_after_partial_write(_payload, stream, **_options):
        stream.write('{"partial":')
        raise OSError("injected disk write failure")

    monkeypatch.setattr(result_service.json, "dump", fail_after_partial_write)
    assert not panel.saveReport(str(target), overwrite)
    assert "injected disk write failure" in panel.error
    if overwrite:
        assert target.read_text(encoding="utf-8") == "keep existing complete report"
    else:
        assert not target.exists()


@pytest.mark.qt
def test_initial_prepare_error_is_visible_from_toolbar(app, tmp_path):
    from PySide6.QtCore import QObject
    from PySide6.QtQuick import QQuickItem
    from tests.film_structure_fixtures import fixture
    from tests.test_film_optimization_restore_layout import click, optimizer, window_runtime

    service, _library, design = fixture(tmp_path)
    design["front_layers"] = []
    with optimizer(service, design) as (owner, _source, _session):
        with window_runtime(app, owner) as (window, warnings):
            button = window.findChild(QQuickItem, "filmSensitivityOpenButton")
            dialog = window.findChild(QObject, "filmSensitivityDialog")
            assert button is not None and dialog is not None
            click(app, window, button)
            assert owner.sensitivityController.error
            assert dialog.property("visible") or owner.error, "Preparation error is hidden in an unopened panel"
            assert not warnings, warnings


@pytest.mark.qt
def test_resource_rejection_prevents_admission_without_dropping_conditions(harness, monkeypatch):
    from coat_sim.core import film_sensitivity
    panel = harness.sensitivity
    monkeypatch.setattr(film_sensitivity, "MAX_RETAINED_RESULT_BYTES", 1)
    assert not panel.start("analyze")
    assert "bytes exceeds" in panel.error and "no scenarios or conditions were dropped" in panel.error
    assert not harness.pool.tasks and not harness.client.submissions


@pytest.mark.qt
@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_terminal_scheduler_authority_prevents_apply_of_otherwise_valid_partial_result(harness, terminal):
    panel = harness.sensitivity
    layer_id = harness.design["front_layers"][0]["id"]
    assert harness.owner.setVariableBound("front", layer_id, "lower_nm", 60.)
    assert harness.owner.setVariableBound("front", layer_id, "upper_nm", 145.)
    assert panel.setOption("max_evaluations", "2")
    submit = harness.client.submit

    def terminal_after_valid_execution(*args, **kwargs):
        row = submit(*args, **kwargs)
        assert row["result"]["state"] == "partial"
        harness.client.rows[row["job_id"]]["state"] = terminal
        return deepcopy(harness.client.rows[row["job_id"]])

    harness.client.submit = terminal_after_valid_execution
    assert panel.start("optimize")
    harness.pool.run()
    assert panel.hasResult, panel.error
    assert panel._payload["state"] == terminal
    assert not panel.canApply and not panel.applyBest()
    assert not harness.source.applied


@pytest.mark.qt
def test_history_summary_separates_actual_percent_points_from_normalized_error(harness):
    service, request = problem(operation='analyze')
    request = replace(request, objectives=tuple(replace(objective, normalization_scale=3.)
                                               for objective in request.objectives))
    job = jobs.build_job_payload(service, request)
    result = jobs.execute_job({'payload': job}, Event(), lambda _item: None,
        'memory', lambda _path, _value: None)
    harness.sensitivity.load_verified_history(result, 'scaled-history')
    normalized = result['best_analysis']['nominal_max_normalized_error']
    raw = max(row['normalized_violation'] * request.objectives[row['objective_index']].normalization_scale
              for row in result['best_analysis']['nominal_conditions'])
    assert raw > 0 and raw != normalized
    summary = harness.sensitivity.resultSummary
    assert f'原始最坏违规 {raw * 100:.7g} 个百分点' in summary
    assert f'最大归一化违规 {normalized:.8g}' in summary
