from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import time

import numpy as np
import pytest

from coat_sim.core.api import CoatService
from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest, freeze_film_synthesis_request
from coat_sim.core.film_design import new_design
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
from coat_sim.core.film_synthesis_jobs import (
    AUDITED_CHECKPOINT_KIND, build_job_payload, canonical_parameters,
    execute_job, is_synthesis_result, validate_checkpoint,
)


class _Materials:
    def __init__(self, rows):
        self.rows = {row["id"]: deepcopy(row) for row in rows}

    def _records(self):
        return deepcopy(list(self.rows.values()))

    def get(self, identifier):
        return deepcopy(self.rows[identifier])


class _FreezeService:
    validate = staticmethod(__import__("coat_sim.core.film_design", fromlist=["validate_design"]).validate_design)
    freeze_design = FilmDesignService.freeze_design

    def __init__(self, records):
        self.materials = _Materials(records)


def _request():
    records = []
    for name, n in (("glass", 1.5), ("low", float(np.sqrt(1.5)))):
        records.append({"id": name, "name": name, "design_ready": True, "optical_sets": [{
            "id": "nk", "name": name, "kind": "tabulated", "notes": "test data",
            "source": "synthetic fixture", "wavelength_unit": "nm",
            "wavelength_min": 500., "wavelength_max": 600.,
            "wavelengths": [500., 550., 600.], "n": [n, n, n], "k": [0., 0., 0.]}]})
    environment = new_design(design_id="scheduler-synthesis-fixture")
    environment["substrate"].update(material_id="glass", optical_set_id="nk")
    seed = deepcopy(environment)
    seed["front_layers"] = [{"id": "known-ar-layer", "material_id": "low", "optical_set_id": "nk",
        "thickness": {"value": 550. / (4. * np.sqrt(1.5)), "unit": "nm"}}]
    request = FilmSynthesisRequest(environment=environment,
        objectives=(FilmOptimizationObjective(550., 550., 1, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=0.001)),),
        candidate_materials=(FilmNeedleMaterialReference("low", "nk"),),
        max_cycles=1, max_evaluations=12, position_samples=1,
        seed_designs=(seed,), design_revision=41)
    return freeze_film_synthesis_request(_FreezeService(records), request)


def _payload(*, include_seed=True):
    request = _request()
    if not include_seed:
        request = replace(request, seed_designs=())
    return film_synthesis_request_to_payload("scheduler-synthesis-known-ar", request)


def _cancel_payload():
    records = []
    records.append({"id": "glass", "name": "glass", "design_ready": True, "optical_sets": [{
        "id": "nk", "name": "glass varying substrate", "kind": "tabulated", "notes": "test data",
        "source": "synthetic fixture", "wavelength_unit": "nm", "wavelength_min": 500.,
        "wavelength_max": 600., "wavelengths": [500., 550., 600.],
        "n": [1.5, 2.5, 1.5], "k": [0., 0., 0.]}]})
    records.append({"id": "low", "name": "low", "design_ready": True, "optical_sets": [{
        "id": "nk", "name": "low", "kind": "tabulated", "notes": "test data",
        "source": "synthetic fixture", "wavelength_unit": "nm", "wavelength_min": 500.,
        "wavelength_max": 600., "wavelengths": [500., 550., 600.],
        "n": [float(np.sqrt(1.5))] * 3, "k": [0., 0., 0.]}]})
    environment = new_design(design_id="scheduler-synthesis-unsolved")
    environment["substrate"].update(material_id="glass", optical_set_id="nk")
    request = FilmSynthesisRequest(environment=environment,
        objectives=(FilmOptimizationObjective(500., 600., 2, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=0.06)),),
        candidate_materials=(FilmNeedleMaterialReference("low", "nk"),),
        max_cycles=None, max_evaluations=None, position_samples=1,
        # This real transport test needs an ongoing task through the first
        # heartbeat. Hybrid local sessions may now finish unmet before1s;
        # their audited cancel result is covered separately.
        search_strategy="legacy", stagnation_cycles=100000)
    frozen = freeze_film_synthesis_request(_FreezeService(records), request)
    return film_synthesis_request_to_payload("scheduler-synthesis-unsolved", frozen)


def test_registered_estimate_and_submit_are_bound_and_reveal_business_root(tmp_path, monkeypatch):
    from coat_sim.mcp import server
    root = tmp_path / "configured-business-root"
    service = CoatService(root)
    monkeypatch.setattr(server, "service", service)
    payload = _payload()

    async def run():
        registered = {tool.name: tool for tool in await server.mcp.list_tools()}
        schema = registered["estimate_compute_task"].input_schema
        assert schema["additionalProperties"] is False
        estimate_result = await server.mcp.call_tool("estimate_compute_task", {
            "kind": "film_synthesis", "parameters": {"payload": payload}})
        estimate = getattr(estimate_result, "structured_content", None)
        if estimate is None:
            estimate = estimate_result.structuredContent
        assert estimate["execution_project_root"] == str(root.resolve())
        assert estimate["estimated_s"] is None
        assert estimate["operations"]["max_evaluations"] == 12
        submission = await server.mcp.call_tool("submit_film_synthesis", {
            "payload": payload, "client_request_id": "known-ar-client"})
        receipt = getattr(submission, "structured_content", None)
        if receipt is None:
            receipt = submission.structuredContent
        return receipt

    receipt = asyncio.run(run())
    assert receipt["state"] == "prepared"
    assert receipt["execution_project_root"] == str(root.resolve())

    assert receipt["client_request_id"] == "known-ar-client"

    async def reject_root_override():
        return await server.mcp.call_tool("submit_film_synthesis", {
            "payload": payload, "client_request_id": "forged-root", "root": str(tmp_path)})
    with __import__("pytest").raises(Exception):
        asyncio.run(reject_root_override())


def test_generic_estimate_cannot_inject_unverified_structure_provenance(tmp_path):
    from coat_sim.core.film_synthesis_jobs import (
        canonical_parameters, deserialize_job_payload,
    )

    service = CoatService(tmp_path / "metadata-root")
    claimed = [{"index": 0, "mechanism": "multistack", "name": "claimed source",
                "metadata": {"doi": "10.0000/fake", "approximation": "claimed exact"}}]
    with pytest.raises(ValueError, match="metadata"):
        canonical_parameters(service, {"payload": _payload(), "physical_structure_metadata": claimed})
    typed = build_job_payload(canonical_parameters(service, {"payload": _payload()}))
    typed["physical_structure_metadata"] = claimed
    with pytest.raises(ValueError, match="metadata"):
        deserialize_job_payload(typed)


def test_scheduler_executor_returns_audited_seed_layer_and_replays_checkpoint(tmp_path):
    payload = _payload()
    parameters = canonical_parameters(None, {"payload": payload})
    job = build_job_payload(parameters)
    checkpoints = []
    progress = []

    def write_json(path: Path, value):
        checkpoints.append((Path(path), deepcopy(value)))

    result = execute_job({"payload": job}, __import__("threading").Event(),
        lambda value, **_kwargs: progress.append(deepcopy(value)),
        tmp_path / "best-candidate.json", write_json, str(tmp_path.resolve()))
    assert is_synthesis_result(result)
    assert result["execution_project_root"] == str(tmp_path.resolve())
    assert result["result"]["state"] == "completed"
    assert result["result"]["targets_met"] is True
    assert result["review"]["layers"][0]["material_id"] == "low"
    assert result["review"]["cost"]["audit_physical_evaluations"] > 0
    assert any(item.get("score_scope") == "audited" for item in progress)
    checkpoint = next((value for _, value in reversed(checkpoints)
                       if value.get("kind") == AUDITED_CHECKPOINT_KIND), None)
    assert checkpoint is not None
    recovered = validate_checkpoint(checkpoint, terminal_state="cancelled")
    assert recovered["result"]["state"] == "cancelled"
    assert recovered["result"]["validation_complete"] is True
    assert recovered["result"]["candidate_design"] == checkpoint["design"]
    assert recovered["result"]["design_revision"] == 41


def test_real_scheduler_worker_persists_success_and_mcp_returns_layer_table(tmp_path, monkeypatch):
    from coat_sim.core.film_synthesis_jobs import canonical_parameters
    from coat_sim.scheduler.client import SchedulerClient
    from coat_sim.scheduler.jobs import freeze_spec
    from coat_sim.mcp import server

    root = tmp_path / "worker-root"
    service = CoatService(root)
    monkeypatch.setattr(server, "service", service)
    client = SchedulerClient(root)
    job_id = "film-synthesis-worker-known-ar"
    try:
        payload = _payload(include_seed=False)
        values = canonical_parameters(service, {"payload": payload})
        spec = freeze_spec(service, "film_synthesis", values)
        queued = client.submit(spec, job_id=job_id, metadata={
            "summary": "Known single-layer antireflection synthesis",
            "execution_project_root": str(root.resolve())})
        assert queued["state"] in {"queued", "running"}
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            state = client.status(job_id)
            if state["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(.15)
        assert state["state"] == "succeeded", state

        async def read_result():
            status = await server.mcp.call_tool("calculation_job_status", {"job_id": job_id})
            status_value = getattr(status, "structured_content", None)
            if status_value is None:
                status_value = json.loads(status.content[0].text)
            result = await server.mcp.call_tool("calculation_job_result", {"job_id": job_id})
            result_value = getattr(result, "structured_content", None)
            if result_value is None:
                result_value = json.loads(result.content[0].text)
            return status_value, result_value

        status_value, result_value = asyncio.run(read_result())
        assert status_value["kind"] == "film_synthesis"
        assert status_value["execution_project_root"] == str(root.resolve()), status_value
        assert result_value["execution_project_root"] == str(root.resolve())
        assert result_value["review"]["layers"][0]["material_id"] == "low"
        assert result_value["review"]["layers"][0]["thickness_nm"] > 0
        assert result_value["result"]["validation_complete"] is True
        assert result_value["result"]["cycles"] >= 1
        assert result_value["result"]["frozen_request"]["seed_designs"] == []
        assert result_value["review"]["layers"]
        assert result_value["best_candidate_audit"]["available"] is True
        assert result_value["best_candidate_audit"]["targets_met"] is True
        assert result_value["best_candidate_audit"]["continuous_certificate"] is False
    finally:
        client.stop()


def test_real_worker_heartbeat_then_cancel_retains_complete_best_and_truthful_audit(tmp_path, monkeypatch):
    from coat_sim.core.film_synthesis_jobs import canonical_parameters
    from coat_sim.core.film_synthesis_archive import physical_design_hash
    from coat_sim.scheduler import daemon
    from coat_sim.scheduler.jobs import freeze_spec
    from coat_sim.mcp import server
    from tests.scheduler_queue_support import SchedulerHarness
    import sys

    root = tmp_path / "cancel-worker-root"
    service = CoatService(root)
    monkeypatch.setattr(server, "service", service)
    harness = SchedulerHarness(root)
    client = harness.client()
    monkeypatch.setattr('coat_sim.scheduler.SchedulerClient', harness.client)
    job_id = "film-synthesis-worker-cancel-audited"
    checkpoint_path = root / "artifacts/runtime/scheduler/jobs" / job_id / "best-candidate.json"
    directory = checkpoint_path.parent
    original_command = daemon.module_command
    def gated_command(module, *arguments):
        if module == 'coat_sim.scheduler' and 'worker' in arguments:
            return [sys.executable, '-m', 'tests.film_synthesis_cancel_worker',
                    '--gate-mode', 'any_complete', *arguments]
        return original_command(module, *arguments)
    monkeypatch.setattr(daemon, 'module_command', gated_command)
    try:
        values = canonical_parameters(service, {"payload": _cancel_payload()})
        spec = freeze_spec(service, "film_synthesis", values)
        client.submit(spec, job_id=job_id, metadata={
            "summary": "Unsolved synthesis cancellation fixture",
            "execution_project_root": str(root.resolve())})
        deadline = time.monotonic() + 40
        state = None
        observed = False
        while time.monotonic() < deadline:
            harness.tick()
            state = client.status(job_id)
            progress = state.get("progress") or {}
            if ((directory / 'cancel-boundary-ready.json').is_file() and checkpoint_path.is_file()
                    and progress.get("heartbeat_sequence", 0) >= 1
                    and progress.get("score_scope") == "audited"):
                observed = True
                break
            if state.get("state") in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(.08)
        assert observed, state
        assert state is not None
        assert state['state'] == 'running' and state['cancel_requested'] is False
        assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["checkpoint_state"] == "audited"
        client.cancel(job_id)
        assert client.status(job_id)['cancel_requested'] is True
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            harness.tick()
            state = client.status(job_id)
            if state["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(.1)
        assert state is not None
        assert state["state"] == "cancelled", state
        async def read_result():
            response = await server.mcp.call_tool("calculation_job_result", {"job_id": job_id})
            value = getattr(response, "structured_content", None)
            return value if value is not None else json.loads(response.content[0].text)

        result = asyncio.run(read_result())
        assert is_synthesis_result(result)
        raw = result["result"]
        assert raw["state"] == "cancelled" and raw["targets_met"] is False
        provenance = raw["candidate_provenance"]
        assert provenance["selection"] == "completed_training_at_cancel"
        assert provenance["training_complete"] is True
        assert provenance["frozen_at_cancel"] is True
        assert provenance["fully_scored_evaluations"] >= 1
        assert raw["candidate_design"] is not None and raw["training_score"] is not None
        assert raw["validation_complete"] is provenance["validation_matches_candidate"]
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        # Cancellation preserves the complete training best, which may be newer
        # than the saved audit. It must not relabel that audit as another design.
        assert tuple(raw["training_score"]["key"]) <= tuple(checkpoint["training_score"]["key"])
        if raw["validation_complete"]:
            assert raw["validation_score"] is not None
            assert physical_design_hash(raw['candidate_design']) == physical_design_hash(raw['validation_score']['design'])
            assert result["best_candidate_audit"]["available"] is True
            assert result["best_candidate_audit"]["targets_met"] is False
        else:
            assert raw["validation_score"] is None
            assert result["best_candidate_audit"]["available"] is False
            assert result["best_candidate_audit"]["targets_met"] is None
            assert all(row["score_scope"] == "training" and row["audit_available"] is False
                       for row in result["review"]["per_objective"])
        assert len(result["review"]["layers"]) == len(raw["candidate_design"]["front_layers"])
        assert raw["performance"]["cleanup_complete"] is True
        assert result["execution_project_root"] == str(root.resolve())
        boundary = json.loads((directory / 'cancel-boundary-final.json').read_text(encoding='utf-8'))
        assert boundary == dict(gated=True, cancel_seen=True, audits_after_cancel=0, forwards_after_cancel=0)
    finally:
        harness.close()
