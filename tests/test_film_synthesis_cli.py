from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import numpy as np

from coat_sim.core.film_auto_synthesis import (FilmSynthesisRequest, freeze_film_synthesis_request,
                                               synthesize_film_design)
from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_needle import (FilmNeedleMaterialReference,
                                                   _candidate_payload_from_materials)
from coat_sim.core.film_synthesis_io import (
    film_synthesis_request_from_payload, film_synthesis_request_to_payload,
    film_synthesis_result_to_payload,
)


class _Materials:
    def __init__(self, records):
        self._rows = {row["id"]: deepcopy(row) for row in records}

    def _records(self):
        return deepcopy(list(self._rows.values()))

    def get(self, identifier):
        return deepcopy(self._rows[identifier])


class _Service:
    validate = staticmethod(validate_design)
    freeze_design = FilmDesignService.freeze_design

    def __init__(self, records):
        self.materials = _Materials(records)


def _problem(*, difficult=False):
    records = []
    for name, n in (("glass", 1.5), ("low", np.sqrt(1.5)), ("high", 2.2)):
        optical = {"id": "nk", "name": name, "kind": "tabulated", "notes": "synthetic fixture",
                   "source": "synthetic test fixture", "wavelength_unit": "nm",
                   "wavelength_min": 450., "wavelength_max": 700.,
                   "wavelengths": [450., 550., 700.], "n": [float(n)] * 3, "k": [0., 0., 0.]}
        records.append({"id": name, "name": name, "design_ready": True, "optical_sets": [optical]})
    environment = new_design(design_id="cli-fixture")
    environment["substrate"].update(material_id="glass", optical_set_id="nk")
    target = FilmOptimizationObjective(540., 560., 3, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=0.0 if difficult else 0.1))
    request = FilmSynthesisRequest(environment, (target,),
        (FilmNeedleMaterialReference("low", "nk"), FilmNeedleMaterialReference("high", "nk")),
        max_cycles=4 if difficult else 1, max_evaluations=80 if difficult else 10,
        position_samples=1)
    return freeze_film_synthesis_request(_Service(records), request)


def _run_cli(source, output, *extra):
    command = [sys.executable, "-m", "coat_sim.film_synthesis_cli", "--request", str(source),
               "--output", str(output), *map(str, extra)]
    return subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                          capture_output=True, text=True, timeout=30)


def test_review_separates_disjoint_objective_windows_and_nm_from_independent_k_um():
    request = _problem()
    result = synthesize_film_design(None, request)
    snapshot = deepcopy(result.frozen_request.frozen_material_snapshot)
    assert snapshot is not None
    optical = next(row for row in next(item for item in snapshot["materials"]
        if item["id"] == "low")["optical_sets"] if row["id"] == "nk")
    optical.pop("wavelengths")
    optical.pop("n")
    optical.pop("k")
    optical.update(kind="rii_formula", formula_number=5, coefficients=[1.7, .001, -2.],
        wavelength_unit="nm", wavelength_min=400., wavelength_max=700., source="n formula source",
        k_data={"wavelength_unit": "um", "wavelength_min": .5, "wavelength_max": .6,
                "wavelengths": [.5, .55, .6], "k": [.01, .02, .03],
                "source": "independent k source", "notes": "separate k data"})
    objectives = (replace(request.objectives[0], wavelength_min_nm=540., wavelength_max_nm=550.),
                  replace(request.objectives[0], wavelength_min_nm=650., wavelength_max_nm=660.))
    frozen = replace(result.frozen_request, objectives=objectives, frozen_material_snapshot=snapshot)
    design = deepcopy(request.environment)
    design["front_layers"] = [{"id": "review-layer", "material_id": "low", "optical_set_id": "nk",
                                "thickness": {"value": 50., "unit": "nm"}}]
    review_result = replace(result, candidate_design=design, frozen_request=frozen)
    payload = film_synthesis_result_to_payload("review-unit", review_result)
    review = payload["review"]
    assert payload["best_candidate_audit"]["available"] is True
    # Scores from the prior objective/candidate do not prove the new windows.
    assert payload["best_candidate_audit"]["targets_met"] is False
    assert payload["best_candidate_audit"]["solver_acceptable"] is False
    assert payload["best_candidate_audit"]["continuous_certificate"] is False
    assert payload["best_candidate_audit"]["audit_max_normalized_error"] <= frozen.acceptable_normalized_error
    assert review["audit_domain"]["objective_windows"] == [
        {"objective_index": 0, "wavelength_min_nm": 540., "wavelength_max_nm": 550., "discrete_line": False},
        {"objective_index": 1, "wavelength_min_nm": 650., "wavelength_max_nm": 660., "discrete_line": False}]
    assert review["audit_domain"]["continuous_span_claimed"] is False
    layer = review["layers"][0]
    assert layer["n_wavelength_domain_nm"] == [400., 700.]
    assert layer["k_wavelength_domain_nm"] == [500., 600.]
    assert layer["material_source"]["n_source"] == "n formula source"
    assert layer["material_source"]["k_source"] == "independent k source"

    unverified = replace(review_result, state="cancelled", termination_reason="cancelled",
                         targets_met=False, validation_complete=False, validation_score=None)
    no_audit = film_synthesis_result_to_payload("review-no-audit", unverified)
    assert no_audit["result"]["targets_met"] is False
    assert no_audit["best_candidate_audit"]["available"] is False
    assert no_audit["best_candidate_audit"]["targets_met"] is None


def test_plan_rejects_frozen_seed_with_independent_k_domain_outside_objectives(tmp_path):
    request = _problem()
    bad_optical = {"id": "n-broad-k-narrow", "name": "n broad / k narrow",
        "kind": "rii_formula", "formula_number": 5, "coefficients": [1.7, .001, -2.],
        "wavelength_unit": "nm", "wavelength_min": 450., "wavelength_max": 700.,
        "source": "test n source",
        "k_data": {"wavelength_unit": "nm", "wavelength_min": 600., "wavelength_max": 700.,
                   "wavelengths": [600., 650., 700.], "k": [0., 0., 0.],
                   "source": "test independent k source"}}
    service = _Service([{"id": "bad-seed", "name": "bad seed", "design_ready": True,
                         "optical_sets": [bad_optical]}])
    snapshot = _candidate_payload_from_materials(service, request.frozen_material_snapshot,
        [FilmNeedleMaterialReference("bad-seed", "n-broad-k-narrow")])
    seed = deepcopy(request.environment)
    seed["front_layers"] = [{"id": "out-of-domain-seed", "material_id": "bad-seed",
        "optical_set_id": "n-broad-k-narrow", "thickness": {"value": 50., "unit": "nm"}}]
    request = replace(request, frozen_material_snapshot=snapshot, seed_designs=(seed,))
    payload = film_synthesis_request_to_payload("plan-k-domain", request)
    plan_file = tmp_path / "invalid-plan.json"
    plan_file.write_text(json.dumps(payload), encoding="utf-8")
    completed = subprocess.run([sys.executable, "-m", "coat_sim.film_synthesis_cli",
        "--request", str(plan_file), "--plan"], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30)
    assert completed.returncode == 2
    assert "strict synthesis validation failed" in completed.stdout
    assert "k" in completed.stdout.lower()


def test_frozen_payload_roundtrip_and_real_cli_cancel_heartbeat_and_exclusive_output(tmp_path):
    easy = _problem()
    spec = {"mechanism": "composite", "name": "one quarter-wave layer",
            "bindings": [{"role": "L", "material_id": "low", "optical_set_id": "nk"}],
            "sections": [{"type": "layer", "role": "L", "wavelength_nm": 550.,
                          "optical_thickness_waves": 0.25}], "validation_wavelengths_nm": []}
    payload = film_synthesis_request_to_payload("cli-roundtrip", easy, [spec])
    request_id, replay, structures = film_synthesis_request_from_payload(json.loads(json.dumps(payload)))
    assert request_id == "cli-roundtrip"
    assert structures[0]["sections"][0]["type"] == "layer"
    assert asdict(replay) == asdict(easy)

    from coat_sim.mcp import server as mcp_server
    from coat_sim.core.mcp_contracts import FILM_SYNTHESIS_PLAN_INPUT_SCHEMA
    from coat_sim.agent.runtime import EXECUTION_TOOLS, allowed_project_tools
    async def inspect_and_plan():
        tools = {item.name: item for item in await mcp_server.mcp.list_tools()}
        tool = tools["plan_film_synthesis"]
        schema = tool.input_schema if hasattr(tool, "input_schema") else tool.inputSchema
        assert schema == FILM_SYNTHESIS_PLAN_INPUT_SCHEMA
        assert "plan_film_synthesis" in allowed_project_tools("chat")
        assert "plan_film_synthesis" in EXECUTION_TOOLS["simulation"]
        response = await mcp_server.mcp.call_tool("plan_film_synthesis", {"payload": payload})
        value = (response.structured_content if hasattr(response, "structured_content")
                 else response.structuredContent)
        if value is None:
            value = response
        return value
    assistant_plan = asyncio.run(inspect_and_plan())
    assert assistant_plan["ok"]

    source, output, checkpoint = (tmp_path / "request.json", tmp_path / "result.json",
                                 tmp_path / "best-audited.json")
    source.write_text(json.dumps(payload), encoding="utf-8")
    completed = _run_cli(source, output, "--checkpoint", checkpoint)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["result"]["state"] == "completed"
    assert result["review"]["per_objective"][0]["targets_met"]
    assert result["physical_structure_metadata"][0]["mechanism"] == "composite"
    assert result["review"]["cost"]["audit_physical_evaluations"] > 0
    snapshot = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert snapshot["checkpoint_state"] == "audited"
    assert snapshot["audit"] and snapshot["design"] == result["result"]["candidate_design"]
    cli_plan = subprocess.run([sys.executable, "-m", "coat_sim.film_synthesis_cli", "--request",
        str(source), "--plan"], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30)
    assert cli_plan.returncode == 0, cli_plan.stdout + cli_plan.stderr
    planned_payload = json.loads(cli_plan.stdout)
    assert assistant_plan["request"] == planned_payload["request"]
    assert assistant_plan["physical_structure_metadata"] == planned_payload["physical_structure_metadata"]
    before = output.read_bytes()
    refused = _run_cli(source, output)
    assert refused.returncode == 2
    assert output.read_bytes() == before
    source_before = source.read_bytes()
    aliased = _run_cli(source, tmp_path / "alias-output.json", "--checkpoint", source)
    assert aliased.returncode == 2
    assert source.read_bytes() == source_before
    bad_checkpoint = tmp_path / "bad-checkpoint.json"
    bad_checkpoint.write_text(json.dumps({"kind": "coat_sim.film_synthesis_result",
                                          "request_id": "another-run",
                                          "checkpoint_state": "audited"}), encoding="utf-8")
    mismatch = _run_cli(source, tmp_path / "mismatch-output.json", "--checkpoint", bad_checkpoint)
    assert mismatch.returncode == 2
    assert json.loads(bad_checkpoint.read_text(encoding="utf-8"))["request_id"] == "another-run"
    nonfinite = _run_cli(source, tmp_path / "nan-output.json", "--heartbeat-interval", "nan")
    assert nonfinite.returncode == 2 and "must be positive" in nonfinite.stderr

    request = _problem(difficult=True)
    # Keep the process running until we observe actual monitoring and an audited
    # incumbent; a fixed startup delay can cancel before the first heartbeat.
    objective = request.objectives[0]
    request = replace(request, max_cycles=None, max_evaluations=None,
        objectives=(replace(objective, pointwise=replace(objective.pointwise, upper_bound=0., lower_bound=None)),
                    replace(objective, pointwise=replace(objective.pointwise, upper_bound=None, lower_bound=1.))))
    cancel_source, cancelled_output, cancel_file = (tmp_path / "cancel-request.json",
        tmp_path / "cancelled-result.json", tmp_path / "cancel")
    cancel_checkpoint = tmp_path / "cancel-checkpoint.json"
    cancel_progress = tmp_path / "cancel-progress.jsonl"
    cancel_source.write_text(json.dumps(film_synthesis_request_to_payload("cli-cancel", request)), encoding="utf-8")
    command = [sys.executable, "-m", "coat_sim.film_synthesis_cli", "--request", str(cancel_source),
               "--output", str(cancelled_output), "--cancel-file", str(cancel_file),
               "--checkpoint", str(cancel_checkpoint),
               "--heartbeat-interval", "0.01", "--stalled-after-seconds", "0.02",
               "--unresponsive-after-seconds", "0.05"]
    observed_heartbeat = False
    events: list[dict[str, Any]] = []
    with cancel_progress.open("w", encoding="utf-8") as progress_stream:
        process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE, stderr=progress_stream, text=True)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and process.poll() is None:
                events = []
                for line in cancel_progress.read_text(encoding="utf-8").splitlines():
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass  # Last line may still be in the process of writing.
                observed_heartbeat = any(item.get("kind") == "heartbeat" for item in events)
                if observed_heartbeat and cancel_checkpoint.exists():
                    break
                time.sleep(.01)
        finally:
            cancel_file.write_text("cancel", encoding="utf-8")
            try:
                stdout, _ = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.communicate(timeout=10)
                raise
    assert observed_heartbeat, cancel_progress.read_text(encoding="utf-8")
    assert cancel_checkpoint.exists()
    assert process.returncode == 130, stdout
    cancelled = json.loads(cancelled_output.read_text(encoding="utf-8"))
    snapshot = json.loads(cancel_checkpoint.read_text(encoding="utf-8"))
    from coat_sim.core.film_synthesis_jobs import is_synthesis_result

    assert cancelled["ok"] and is_synthesis_result(cancelled)
    raw = cancelled["result"]
    assert raw["state"] == "cancelled" and not raw["targets_met"]
    provenance = raw["candidate_provenance"]
    assert provenance["selection"] == "completed_training_at_cancel"
    assert provenance["training_complete"] is True and provenance["frozen_at_cancel"] is True
    assert provenance["fully_scored_evaluations"] >= 1
    assert raw["candidate_design"] is not None
    assert tuple(raw["training_score"]["key"]) <= tuple(snapshot["training_score"]["key"])
    assert raw["validation_complete"] is provenance["validation_matches_candidate"]
    if raw["validation_complete"]:
        assert raw["validation_score"] is not None
        assert cancelled["best_candidate_audit"]["available"] is True
    else:
        assert raw["validation_score"] is None
        assert cancelled["best_candidate_audit"]["available"] is False
        assert cancelled["best_candidate_audit"]["targets_met"] is None
    assert raw["performance"]["cleanup_complete"] is True
