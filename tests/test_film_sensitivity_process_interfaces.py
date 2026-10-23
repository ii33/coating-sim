"""Process-model fields reach the published interface and frozen job worker.

These tests cover the MCP schema and direct read-only business planner, not MCP
transport or any local scheduler endpoint. Frozen job evaluation is the real
typed ``film_sensitivity`` executor, with no numerical mocks.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
import json

import pytest

from coat_sim.core import film_sensitivity_jobs as jobs
from coat_sim.core.mcp_contracts import (
    FILM_SENSITIVITY_PLAN_INPUT_SCHEMA,
    validate_mcp_arguments,
)
from coat_sim.core.film_sensitivity import (
    FilmSensitivityScenario,
    FilmThicknessError,
    film_sensitivity_request_from_payload,
    film_sensitivity_request_to_payload,
    film_sensitivity_result_from_payload,
    film_sensitivity_result_to_payload,
    freeze_film_sensitivity_request,
    validate_film_sensitivity_result_payload,
)
from coat_sim.scheduler import freeze_spec
from coat_sim.scheduler.jobs import JobSpec
from tests.test_film_sensitivity import problem
from tests.test_film_sensitivity_constrained import feasible_problem

pytestmark = pytest.mark.algorithm


def _thickened_process_request(*, operation="analyze"):
    service, request = problem(operation=operation, objective="finite_scenario_expected")
    request = replace(request,
        errors=(FilmThicknessError("low-film", magnitude=.01, unit="relative_fraction",
                                   absolute_nm=.5, relative_fraction=.02),),
        scenarios=(FilmSensitivityScenario("sigma-1.5", (1.5,)),),
        error_amplitude_semantics="standard_deviation",
        robustness_objective="finite_scenario_expected")
    return service, request


def _frozen_job(service, request):
    frozen = freeze_film_sensitivity_request(service, request)
    raw = film_sensitivity_request_to_payload(frozen)
    spec_dict = freeze_spec(service, "film_sensitivity", {"payload": raw})
    spec = JobSpec.parse_frozen(spec_dict)
    return frozen, spec


def test_mcp_discovery_schema_exposes_process_models_scales_and_factor_semantics():
    payload_schema = FILM_SENSITIVITY_PLAN_INPUT_SCHEMA["properties"]["payload"]
    props = payload_schema["properties"]
    assert props["optimization_policy"]["enum"] == ["robustness_only", "nominal_preserving"]
    assert props["optimization_policy"]["default"] == "robustness_only"
    assert props["error_amplitude_semantics"]["enum"] == ["bounded_amplitude", "standard_deviation"]
    assert props["error_model"]["enum"] == [
        "independent", "common_mode", "material_common_mode", "linear_drift"]
    error = props["errors"]["items"]["properties"]
    assert set(error) == {"layer_id", "magnitude", "unit", "absolute_nm", "relative_fraction"}
    assert "not RSS-combined" in props["errors"]["description"]
    scenario = props["scenarios"]["items"]["properties"]
    assert "coefficients" in scenario
    assert "never clipped" in props["scenarios"]["description"]

    service, request = _thickened_process_request()
    _ = service
    raw = film_sensitivity_request_to_payload(request)
    # The public schema discovers the full payload. The canonical sensitivity
    # parser remains authoritative for cross-field and closed-world validation.
    assert validate_mcp_arguments(FILM_SENSITIVITY_PLAN_INPUT_SCHEMA, {"payload": raw}) == {"payload": raw}
    assert film_sensitivity_request_from_payload(raw).scenarios[0].coefficients == (1.5,)
    bounded = deepcopy(raw)
    bounded.pop("error_amplitude_semantics")
    with pytest.raises(ValueError, match="bounded-amplitude"):
        film_sensitivity_request_from_payload(bounded)


def test_cli_analyze_serializes_additive_sigma_error_and_uses_explicit_coefficient(tmp_path, capsys):
    from coat_sim import film_sensitivity_cli as cli

    service, request = _thickened_process_request()
    frozen = freeze_film_sensitivity_request(service, request)
    source = tmp_path / "process-request.json"
    source.write_text(json.dumps(film_sensitivity_request_to_payload(frozen)), encoding="utf-8")
    assert cli.main(["--request", str(source)]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"]
    result = film_sensitivity_result_from_payload(envelope["result"])
    assert result.state == "completed"
    assert result.frozen_request.error_amplitude_semantics == "standard_deviation"
    assert result.frozen_request.errors[0].absolute_nm == pytest.approx(.5)
    assert result.frozen_request.errors[0].relative_fraction == pytest.approx(.02)
    assert result.frozen_request.scenarios[0].coefficients == (1.5,)
    assert result.best_analysis is not None
    # 0.01 legacy relative + 0.02 explicit relative and 0.5 nm additive,
    # all under one factor, at the fixture's 80 nm nominal layer thickness.
    assert result.best_analysis.finite_scenarios[0].delta_thickness_nm == pytest.approx((4.35,))
    encoded = film_sensitivity_result_to_payload(result)
    assert film_sensitivity_result_from_payload(json.loads(json.dumps(encoded))) == result
    validate_film_sensitivity_result_payload(encoded, expected_request=frozen)


def test_cli_runs_nominal_preserving_finite_scenario_optimization_end_to_end(tmp_path, capsys):
    from coat_sim import film_sensitivity_cli as cli

    service, request = feasible_problem(objective="finite_scenario_expected")
    # Explicitly add relative process error while retaining a complete frozen
    # finite-scenario request from the constrained-solver acceptance fixture.
    request = replace(request, errors=(FilmThicknessError("low-film", absolute_nm=1.2,
        relative_fraction=.02),), error_model="independent",
        error_amplitude_semantics="bounded_amplitude", optimization_policy="nominal_preserving")
    frozen = freeze_film_sensitivity_request(service, request)
    source = tmp_path / "nominal-preserving-request.json"
    source.write_text(json.dumps(film_sensitivity_request_to_payload(frozen)), encoding="utf-8")
    output = tmp_path / "nominal-preserving-result.json"
    assert cli.main(["--request", str(source), "--output", str(output)]) == 0
    cli_status = json.loads(capsys.readouterr().out)
    assert cli_status["state"] == "completed"
    result_payload = json.loads(output.read_text(encoding="utf-8"))["result"]
    result = film_sensitivity_result_from_payload(result_payload)
    assert result.operation == "optimize"
    assert result.frozen_request.optimization_policy == "nominal_preserving"
    assert result.frozen_request.robustness_objective == "finite_scenario_expected"
    assert result.best_analysis is not None and result.best_analysis.finite_scenarios
    assert result.nominal_targets_met
    validate_film_sensitivity_result_payload(result_payload, expected_request=frozen)


def test_plan_business_and_typed_job_worker_preserve_process_identity_and_replay(monkeypatch):
    from coat_sim.mcp import server

    service, request = _thickened_process_request()
    request = replace(request, optimization_policy="nominal_preserving")
    frozen, spec = _frozen_job(service, request)
    request_payload = spec.parameters["payload"]["request"]
    assert request_payload["optimization_policy"] == "nominal_preserving"
    assert request_payload["error_amplitude_semantics"] == "standard_deviation"
    assert request_payload["error_model"] == "independent"
    assert request_payload["errors"][0]["absolute_nm"] == pytest.approx(.5)
    assert request_payload["errors"][0]["relative_fraction"] == pytest.approx(.02)
    assert request_payload["scenarios"][0]["coefficients"] == [1.5]

    # Import only the registered business adapter, with its service explicitly
    # scoped to the in-memory analytical fixture; do not run MCP transport.
    monkeypatch.setattr(server, "service", SimpleNamespace(film_design_service=service))
    plan = server.plan_film_sensitivity(film_sensitivity_request_to_payload(request))
    assert plan["ok"] and plan["request"] == film_sensitivity_request_to_payload(frozen)

    checkpoints = []
    result_payload = jobs.execute_job(spec.parameters, Event(), lambda _row: None, "checkpoint",
                                      lambda _path, row: checkpoints.append(deepcopy(row)))
    assert jobs.result_matches_job(result_payload, spec.parameters["payload"])
    assert result_payload["state"] == "completed"
    assert checkpoints and checkpoints[-1]["result"]["state"] == "partial"
    assert checkpoints[-1]["result"]["is_final"] is False
    replayed = film_sensitivity_result_from_payload(
        validate_film_sensitivity_result_payload(result_payload,
            expected_request=frozen, replay=True))
    assert replayed.frozen_request == frozen
    assert replayed.best_analysis is not None
    assert replayed.best_analysis.finite_scenarios[0].delta_thickness_nm == pytest.approx((4.35,))

    # Every newly meaningful field is part of the frozen identity, not display
    # metadata. A valid result cannot be replayed against a different request.
    for field, change in (("optimization_policy", "robustness_only"),
                          ("error_amplitude_semantics", "bounded_amplitude"),
                          ("error_model", "common_mode")):
        foreign = deepcopy(spec.parameters["payload"])
        foreign["request"][field] = change
        assert not jobs.result_matches_job(result_payload, foreign)
    foreign = deepcopy(spec.parameters["payload"])
    foreign["request"]["errors"][0]["absolute_nm"] += .1
    assert not jobs.result_matches_job(result_payload, foreign)
    foreign = deepcopy(spec.parameters["payload"])
    foreign["request"]["scenarios"][0]["coefficients"][0] += .1
    assert not jobs.result_matches_job(result_payload, foreign)


def test_initially_infeasible_nominal_preserving_job_fails_without_repair():
    service, request = feasible_problem(objective="finite_scenario_expected")
    request = replace(request,
        objectives=(replace(request.objectives[0], upper_bound=0.),),
        acceptable_normalized_error=1.)
    _frozen, spec = _frozen_job(service, request)
    result = jobs.execute_job(spec.parameters, Event(), lambda _row: None, "checkpoint",
                              lambda _path, _row: None)
    assert result["state"] == "failed"
    assert result["termination_reason"] == "initial_nominal_infeasible"
    assert result["best_analysis"]["nominal_targets_met"] is False
    assert jobs.result_matches_job(result, spec.parameters["payload"])


def test_process_job_budget_and_cancel_keep_real_terminal_outcomes():
    service, request = feasible_problem(objective="finite_scenario_worst")
    frozen, spec = _frozen_job(service, replace(request, max_evaluations=1))
    partial = jobs.execute_job(spec.parameters, Event(), lambda _row: None, "checkpoint",
                               lambda _path, _row: None)
    assert partial["state"] == "partial" and partial["termination_reason"] == "evaluation_limit"
    assert partial["best_analysis"]["nominal_targets_met"] is True
    assert jobs.result_matches_job(partial, spec.parameters["payload"])
    validate_film_sensitivity_result_payload(partial, expected_request=frozen)

    cancelled_event = Event()
    cancelled, cancel_spec = _frozen_job(service, request)
    def report(progress):
        if progress["stage"] == "film_sensitivity:baseline_complete":
            cancelled_event.set()
    cancelled_result = jobs.execute_job(cancel_spec.parameters, cancelled_event, report, "checkpoint",
                                        lambda _path, _row: None)
    assert cancelled_result["state"] == "cancelled"
    assert cancelled_result["termination_reason"] == "cancelled"
    assert cancelled_result["best_analysis"]["nominal_targets_met"] is True
    assert jobs.result_matches_job(cancelled_result, cancel_spec.parameters["payload"])
    validate_film_sensitivity_result_payload(cancelled_result, expected_request=cancelled)


def test_legacy_canonical_payload_omits_new_defaults_and_zero_error_terms():
    service, request = problem()
    payload = film_sensitivity_request_to_payload(request)
    assert "optimization_policy" not in payload
    assert "error_amplitude_semantics" not in payload
    assert all("absolute_nm" not in error and "relative_fraction" not in error
               for error in payload["errors"])
    assert film_sensitivity_request_to_payload(film_sensitivity_request_from_payload(payload)) == payload
    explicit_neutral = replace(request, optimization_policy="robustness_only",
        error_amplitude_semantics="bounded_amplitude",
        errors=tuple(replace(error, absolute_nm=0., relative_fraction=0.) for error in request.errors))
    assert film_sensitivity_request_to_payload(explicit_neutral) == payload
    assert _frozen_job(service, explicit_neutral)[1].fingerprint == _frozen_job(service, request)[1].fingerprint


@pytest.mark.parametrize(("field", "value"), [
    ("optimization_policy", "nominal_preserving"),
    ("error_amplitude_semantics", "standard_deviation"),
])
def test_ordinary_optimization_v1_still_rejects_new_sensitivity_request_fields(field, value):
    from coat_sim.core.film_optimization import FilmOptimizationRequest
    from coat_sim.core.film_optimization_job import deserialize_request, serialize_request

    _service, request = feasible_problem(objective="finite_scenario_expected")
    ordinary = FilmOptimizationRequest(request.design_snapshot, request.design_revision,
        request.variables, request.objectives)
    payload = serialize_request(ordinary)
    payload[field] = value
    with pytest.raises(ValueError, match="fields"):
        deserialize_request(payload)
