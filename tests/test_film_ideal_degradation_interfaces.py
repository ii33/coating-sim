"""Current schema 3 policies through the real typed job, CLI and readonly MCP paths."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import io
import json
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from coat_sim.core import film_ideal_sensitivity as ideal
from coat_sim.core import film_sensitivity_workflow_jobs as jobs
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.scheduler.jobs import JobSpec, freeze_spec
from coat_sim.scheduler.worker import _completed_film_cancel_reply, _film_reply_matches_payload
from tests.test_film_ideal_sensitivity import problem as strict_problem

pytestmark = pytest.mark.algorithm


def problem(*, budget=.1, maximum=100, lower=.99):
    service, original = strict_problem(max_evaluations=maximum, max_iterations=25)
    original = replace(original, objectives=(FilmOptimizationObjective(550., 550., 1, pointwise=FilmObjectiveConstraint(mode='range', target_value=None, lower_bound=lower, upper_bound=None)),))
    request = replace(original,
        policy='bounded_baseline_degradation', objective_ids=('stable-HR-target',),
        budgets=(ideal.FilmIdealDegradationBudget('stable-HR-target', budget, 'percentage_point'),))
    return service, request


def test_real_nominal_failing_guard_candidate_keeps_exact_job_identity_and_checkpoint():
    service, request = problem()
    frozen = jobs.freeze_workflow_request(service, request)
    original = jobs.workflow_request_to_payload(frozen)
    assert original['schema_version'] == 3 and original['policy'] == 'bounded_baseline_degradation'
    assert not any(key.startswith('baseline_') for key in original)
    spec = JobSpec.parse_frozen(freeze_spec(None, 'film_ideal_sensitivity', {'request': frozen}))
    checkpoints: list[tuple[str, dict[str, Any]]] = []
    result = jobs.execute_job(spec.parameters, Event(), lambda _row: None, 'memory',
        lambda path, payload: checkpoints.append((path, deepcopy(payload))))
    parsed = jobs.workflow_result_from_payload(result)
    assert parsed.baseline_analysis is not None and parsed.best_analysis is not None
    assert parsed.state in ('completed', 'partial') and parsed.improved
    assert parsed.best_analysis.objective_value < parsed.baseline_analysis.objective_value
    assert not parsed.baseline_analysis.nominal_targets_met and not parsed.nominal_targets_met
    assert parsed.guard_pass and parsed.usable_candidate
    assert parsed.best_analysis.nominal_conditions[0].actual < parsed.baseline_analysis.nominal_conditions[0].actual
    assert jobs.workflow_request_to_payload(parsed.frozen_request) == original
    assert jobs.result_matches_job(result, spec.parameters['payload'])
    assert _film_reply_matches_payload(result, spec.parameters['payload'])
    jobs.validate_workflow_result(result, expected_request=frozen, replay=True)
    assert checkpoints
    recovered = jobs.validate_checkpoint(checkpoints[-1][1], expected_job=spec.parameters['payload'])
    assert recovered['state'] == 'cancelled'
    jobs.validate_workflow_result(recovered, expected_request=frozen, replay=True)
    cancelled = _completed_film_cancel_reply(result, spec.parameters['payload'])
    assert cancelled is not None and cancelled['state'] == 'cancelled'
    jobs.validate_workflow_result(cancelled, expected_request=frozen, replay=True)
    foreign = deepcopy(spec.parameters['payload'])
    foreign['request']['budgets'][0]['delta'] = .2
    assert not jobs.result_matches_job(result, foreign)
    assert _completed_film_cancel_reply(result, foreign) is None
    with pytest.raises(ValueError, match='identity'):
        jobs.validate_workflow_result(result, expected_request=foreign['request'], replay=True)


def test_freeze_and_plan_do_not_evaluate_or_mutate_the_execution_baseline(monkeypatch):
    from coat_sim.mcp import server
    service, request = problem()
    calls: list[object] = []
    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError('planning may not hide a baseline physical evaluation')
    from coat_sim.core import _film_ideal_sensitivity_core as numerical
    monkeypatch.setattr(numerical, 'simulate_with_thickness_jacobian', forbidden)
    monkeypatch.setattr(server, 'service', SimpleNamespace(film_design_service=service))
    raw = jobs.workflow_request_to_payload(request)
    result = server.plan_film_ideal_sensitivity(raw)
    assert result['ok'] and result['request']['kernel_identity']
    assert result['request']['budgets'] == [dict(objective_id='stable-HR-target', delta=.1, unit='percentage_point')]
    assert not any(key.startswith('baseline_') for key in result['request'])
    assert calls == []
    assert request.kernel_identity is None


def test_mcp_discovery_accepts_only_current_ideal_schema_without_table_fields():
    from coat_sim.core import mcp_contracts as contracts
    from coat_sim.core.capabilities import CapabilityError
    _service, request = problem()
    raw = jobs.workflow_request_to_payload(request)
    schema = contracts.FILM_IDEAL_SENSITIVITY_PLAN_INPUT_SCHEMA
    assert contracts.validate_mcp_arguments(schema, {'payload': raw}) == {'payload': raw}
    for field in ('policy', 'objective_ids', 'budgets'):
        missing = deepcopy(raw)
        missing.pop(field)
        with pytest.raises(CapabilityError):
            contracts.validate_mcp_arguments(schema, {'payload': missing})
        with pytest.raises(ValueError):
            jobs.workflow_request_from_payload(missing)
    missing_unit = deepcopy(raw)
    missing_unit['budgets'][0].pop('unit')
    with pytest.raises(CapabilityError):
        contracts.validate_mcp_arguments(schema, {'payload': missing_unit})
    with pytest.raises(ValueError):
        jobs.workflow_request_from_payload(missing_unit)
    _service, strict = strict_problem()
    current = jobs.workflow_request_to_payload(strict)
    assert current['schema_version'] == 3 and current['policy'] == 'strict_nominal'
    assert contracts.validate_mcp_arguments(schema, {'payload': current}) == {'payload': current}
    old = deepcopy(current)
    old['schema_version'] = 1
    with pytest.raises(CapabilityError):
        contracts.validate_mcp_arguments(schema, {'payload': old})
    with pytest.raises(ValueError, match='unsupported'):
        jobs.workflow_request_from_payload(old)
    table = contracts.FILM_ERROR_TABLE_PLAN_INPUT_SCHEMA['properties']['payload']['properties']
    assert all(field not in table for field in ('policy', 'objective_ids', 'budgets', 'kernel_identity'))


def test_cli_executes_current_ideal_and_reports_partial_not_nominal_success(monkeypatch, capsys, tmp_path):
    from coat_sim import film_sensitivity_workflow_cli as cli
    service, request = problem()
    frozen = jobs.freeze_workflow_request(service, request)
    raw = jobs.workflow_request_to_payload(frozen)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(raw)))
    code = cli.main([])
    result = json.loads(capsys.readouterr().out)['result']
    assert code == {'completed': 0, 'partial': 3, 'failed': 2, 'cancelled': 130}[result['state']]
    parsed = jobs.workflow_result_from_payload(result)
    assert parsed.guard_pass and parsed.usable_candidate and parsed.improved
    assert not parsed.nominal_targets_met and not parsed.targets_met
    jobs.validate_workflow_result(result, expected_request=frozen, replay=True)
    source, output = tmp_path/'request.json', tmp_path/'result.json'
    source.write_text(json.dumps(raw), encoding='utf-8')
    assert cli.main(['--request', str(source), '--output', str(output)]) == code
    stored = output.read_bytes()
    jobs.validate_workflow_result(json.loads(stored)['result'], expected_request=frozen, replay=True)
    assert cli.main(['--request', str(source), '--output', str(output)]) == 2
    assert output.read_bytes() == stored


def test_operation_mismatch_remains_rejected_for_current_ideal():
    service, request = problem()
    frozen = jobs.freeze_workflow_request(service, request)
    job = jobs.build_job_payload(None, frozen)
    payload = jobs.workflow_result_to_payload(ideal.analyze_film_ideal_sensitivity(None, frozen))
    jobs.validate_workflow_result(payload, expected_request=frozen, replay=True)
    assert payload['operation'] == 'analyze'
    assert not jobs.result_matches_job(payload, job)
    assert not _film_reply_matches_payload(payload, job)
    assert _completed_film_cancel_reply(payload, job) is None
